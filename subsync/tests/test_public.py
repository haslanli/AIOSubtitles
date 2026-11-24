"""Public mode: its settings, the pool of users' workers, the shared store's memory per language, and the worker's turn
and limits."""
import base64
import re
import os
import threading
import unittest

from helpers import Clock, closed_after, HAVE_CRYPTOGRAPHY, NEEDS, run_main, run_once, wait_until
from subsync.settings import Settings, available_cpus

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

    def test_a_trusted_proxy_that_is_neither_gateway_nor_an_ip_address_stops_public_mode(self):
        # A request's own address is compared with each entry as written, so a range, a name or a typo never
        # matched, and every visitor counted as the web server's one address for the sign-up limit
        s = Settings({"SUBSYNC_TRUSTED_PROXY": "127.0.0.1, 172.18.0.0/16, caddy", "SUBSYNC_PUBLIC": "on"})
        self.assertEqual(s.conflicts(), [
            "SUBSYNC_TRUSTED_PROXY must be gateway or IP addresses separated by commas, like 172.18.0.1: 172.18.0.0/16 "
            "isn't one.",
            "SUBSYNC_TRUSTED_PROXY must be gateway or IP addresses separated by commas, like 172.18.0.1: caddy isn't "
            "one."])
        self.assertEqual(Settings({"SUBSYNC_TRUSTED_PROXY": "caddy"}).conflicts(), [])          # self-hosted: unused
        self.assertEqual(Settings({"SUBSYNC_TRUSTED_PROXY": "172.18.0.1, fd00::1", "SUBSYNC_PUBLIC": "on"}).conflicts(),
                         [])

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
        # either was a traceback at start instead of the setting's sentence. Another script's digits, which
        # int() reads as if they were ASCII ones ("٣" was 3, "２" 2), are written wrong too, as any other non-number
        for name, field in (("SUBSYNC_SYNCS_AT_ONCE", "syncs_at_once"), ("SUBSYNC_AUDIO_AT_ONCE", "audio_at_once"),
                            ("SUBSYNC_MAX_LANGUAGES", "max_languages")):
            for wrong in ("²", "2²", "1" * 5000, "٣", "２"):
                with self.subTest(name=name, value=wrong[:8]):
                    s = Settings({name: wrong, "SUBSYNC_PUBLIC": "on"})
                    self.assertIsNone(getattr(s, field))
                    self.assertTrue(any(p.startswith(name + " must be a whole number") for p in s.conflicts()))
        self.assertEqual(Settings({"SUBSYNC_MAX_LANGUAGES": "3"}).max_languages, 3)

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
        # its subtitle list only, for the originals its entries serve
        self.assertEqual(aios.subtitle_requests, 1)
        self.assertEqual(app.worker.known_originals(job.alias),
                         {"eng": {"url": "http://127.0.0.1:9/sub.srt", "id": "os-1"}})
        self.assertEqual(self.store.day_count("read"), 0)

    def test_a_known_torrent_file_gets_the_originals_its_entries_serve(self):
        # The known-file path sets the file's originals from the list, as the capped path does, on a
        # self-hosted install and in public mode: a pick entry listed while its job was queued serves the original,
        # and never asks for the list again (each try a new job and stream listing, answered 503); the subtitle made for
        # the stored file's own hash comes first
        from subsync.store import Store
        from subsync.worker import Job, Safety, Worker
        stored = "0123456789abcdef-1000"                                   # a stored file's key: its hash and size
        subs = [{"id": "os-1", "url": "http://127.0.0.1:9/1.srt", "lang": "eng", "release": "", "hash": "",
                 "hash_match": False, "ai": False},
                {"id": "os-2", "url": "http://127.0.0.1:9/2.srt", "lang": "eng", "release": "", "hash": stored[:16],
                 "hash_match": False, "ai": False}]
        for keep_texts in (True, False):
            with self.subTest(keep_texts=keep_texts):
                folder = os.path.join(self.dir, f"keep-{keep_texts}")
                os.makedirs(folder)
                store = closed_after(self, Store(folder, keep_texts=keep_texts))
                s = Settings({"SUBSYNC_DATA": folder, "SUBSYNC_PUBLIC_BASE": "http://x.test",
                              "SUBSYNC_LANGUAGES": "eng"})
                aios = ListingAios({"url": "http://127.0.0.1:9/never",
                                    "streamData": {"torrent": {"infoHash": HASH, "fileIdx": 0}}}, subs)
                w = Worker(s, store, aios, Safety())
                store.alias(f"bt:{HASH}:0", stored)
                store.file_read(stored, 1000, stored[:16], True, {"rule": s.rule_id})
                store.judged_put(stored, ["eng"], s.rule_id)
                job = Job("movie", "tt1", {"filename": "other-name.mkv", "videoSize": 1000})
                w.remember(job)
                w.process(job)
                self.assertEqual(store.resolve(job.alias), stored)
                self.assertEqual(w.known_originals(job.alias),
                                 {"eng": {"url": "http://127.0.0.1:9/2.srt", "id": "os-2"}})
                self.assertEqual((aios.subtitle_requests, store.day_count("read")), (1, 0))
                self.assertEqual(w.wait_for_originals(job.alias, 0), w.known_originals(job.alias))
                self.assertFalse(w.busy_with(job.alias))       # nothing queued again for it

    def test_a_known_torrent_file_of_another_hash_than_the_players_is_not_used(self):
        # A player that sends its file's hash gets a stored file only of that hash; the torrent file
        # a listing names (which the person's own listing asserts) may be another one: their own file is read instead
        from unittest import mock
        from subsync import worker
        from subsync.worker import Job
        stored = "0123456789abcdef-1000"                                   # a stored file's key: its hash and size
        aios = ListingAios({"url": "http://127.0.0.1:9/never", "streamData": {"torrent": {"infoHash": HASH, "fileIdx": 0}}},
                           [{"id": "os-1", "url": "http://127.0.0.1:9/sub.srt", "lang": "eng", "release": "", "hash": "",
                             "hash_match": False, "ai": False}])
        app = self.app("eng", aios)
        self.store.alias(f"bt:{HASH}:0", stored)
        self.store.file_read(stored, 1000, stored[:16], True, {"rule": app.s.rule_id})
        self.store.judged_put(stored, ["eng"], app.s.rule_id)
        other = Job("movie", "tt1", {"videoHash": "fedcba9876543210", "videoSize": 1000})
        with mock.patch.object(worker, "READ_RETRY_S", 0):
            app.worker.process(other)
        self.assertEqual(aios.subtitle_requests, 1)                         # its own read (which fails here)
        self.assertIsNone(self.store.resolve(other.alias))
        same = Job("movie", "tt1", {"videoHash": stored[:16].upper(), "videoSize": 1000})
        app.worker.process(same)                                             # the stored file is its own
        self.assertEqual(aios.subtitle_requests, 2)                         # its list, nothing read
        self.assertEqual(self.store.resolve(same.alias), stored)

    def test_a_file_known_by_its_name_of_another_hash_than_the_players_is_not_listed(self):
        # The same for a file found by the name and size the request gives
        from subsync.worker import Job
        stored = "0123456789abcdef-1000"
        app = self.app("eng")
        self.store.put(stored, "a" * 64, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                       fmt="srt", candidate_id="os-1")
        self.store.alias("name:a.mkv|1000", stored)
        for sent, listed in (("fedcba9876543210", []), (stored[:16], ["English (synced)"]), (None, ["English (synced)"])):
            with self.subTest(videoHash=sent):
                job = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1000, "videoHash": sent})
                self.assertEqual([e["label"] for e in app.offers(job)[1]], listed)

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
    who = rows_key = None

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
                t0 = time.monotonic()
                time.sleep(0.05)
                with lock:
                    spans.append((t0, time.monotonic()))
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
                t0 = time.monotonic()
                time.sleep(0.1)
                with lock:
                    spans.setdefault(key, []).append((t0, time.monotonic()))
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
                t0 = time.monotonic()
                time.sleep(0.15)
                with lock:
                    spans.append((job.alias, t0, time.monotonic()))
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
            t0 = time.monotonic()
            time.sleep(0.6)
            with lock:
                spans[job.alias] = (t0, time.monotonic())
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
            limits.who = limits.rows_key = who
            w = Worker(self.settings(SUBSYNC_PUBLIC="on"), self.store, ListingAios(row), Safety(), limits=limits)
            self.workers.append(w)
            return w
        a, b = person("a" * 16), person("b" * 16)
        job = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})

        def refused(j):
            raise Refused("HTTP 429")
        a.process = refused
        with self.assertLogs("subsync.worker", "WARNING") as logs:
            run_once(a, job)            # one turn of the loop; a job that doesn't run fails the test
        self.assertEqual(logs.output, ["WARNING:subsync.worker:refused movie/tt1 (HTTP 429): no reads for 900 s"])
        self.assertTrue(a._file_blocked(job.alias))          # 1 h, then 6 h, 24 h, for this person
        self.assertFalse(self.store.file_blocked(job.alias))
        b.process(Job("movie", "tt1", dict(job.hints)))
        self.assertEqual(b.aios.subtitle_requests, 1)        # not backing off: the other person's job goes on

    def test_one_persons_failed_usenet_first_read_backs_the_file_off_for_them_only(self):
        """A Usenet file's failed first read is most likely the person's own Usenet service, indexer or
        provider (an NZB fetch that failed can't be told apart): their Usenet stops for the day and the file backs off
        for them, never for another person who reads it through debrid (it did, up to "never"); nothing is written once
        their data is deleted."""
        from unittest import mock
        from subsync.worker import Job, Safety, UsenetFinder, Worker
        subs = [{"id": "os-1", "url": "http://127.0.0.1:9/s.srt", "lang": "eng", "release": "", "hash": "",
                 "hash_match": False, "ai": False}]
        dead = {"url": "http://127.0.0.1:9/never"}

        def person(who, row=None):
            limits = StubLimits()
            limits.who = limits.rows_key = who
            w = Worker(self.settings(SUBSYNC_PUBLIC="on"), self.store, ListingAios(row, subs), Safety(), limits=limits)
            self.workers.append(w)
            return w
        a, b = person("a" * 16), person("b" * 16, dead)
        stops = []
        a.usenet_finder = UsenetFinder(None, mock.Mock(stop=stops.append))
        hints = {"filename": "Movie.2020.1080p.mkv", "videoSize": 4_000_000_000}
        job = Job("movie", "tt1", hints)
        job.usenet = True
        with self.assertLogs("subsync.worker", "INFO") as logs:
            a._process_row(job, dict(hints), dead, None)
        self.assertEqual(stops, ["the first read of a Usenet file failed"])
        self.assertIn("read failed movie/tt1 (", logs.output[-1])
        self.assertTrue(a._file_blocked(job.alias))          # 1 h, then 6 h, 24 h, for this person
        self.assertFalse(self.store.file_blocked(job.alias))
        self.assertFalse(b._file_blocked(job.alias))
        from subsync import worker
        with mock.patch.object(worker, "READ_RETRY_S", 0):
            b.process(Job("movie", "tt1", dict(hints)))
        self.assertEqual(b.aios.subtitle_requests, 1)        # not backing off: the other person's job goes on
        gone = person("c" * 16)                              # their data deleted while the read ran
        gone.usenet_finder = UsenetFinder(None, mock.Mock())
        gone.stop(forget=True)
        rows = self.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        again = Job("movie", "tt1", hints)
        again.usenet = True
        gone._process_row(again, dict(hints), dead, None)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], rows)

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
        # Their entries' address carries their read key, never their personal link
        self.assertEqual(a.s.public_base, "https://subs.test/r/" + self.users.read_key(self.tokens[0]))
        self.assertNotIn(self.tokens[0], a.s.public_base)
        self.assertEqual(a.worker.aios.base, "https://aios.test/stremio/u1/E1")
        self.assertEqual(b.worker.aios.base, "https://aios.test/stremio/u2/E2")
        self.assertEqual(a.worker.aios.password, "p1")
        self.assertIs(a.store, b.store)
        self.assertFalse(a.s.audio_route)
        self.assertIs(self.pool.app(self.tokens[0]), a)          # the same person keeps their app
        self.assertEqual(self.pool.active(), 2)

    def test_a_persons_usenet_config_is_used_by_the_one_rule(self):
        # One rule (Settings.usenet_usable) at the start, in the self-hosted service and here; a record's Usenet
        # config without its password was used anyway, and every Usenet search through it was blocked
        from subsync.public import Pool
        from subsync.users import new_token
        env = dict(self.env, SUBSYNC_USENET="on")
        link = "https://aios.test/stremio/n1/N1/manifest.json"
        both, no_password = new_token(), new_token()
        self.users.create(both, dict(record(), usenet_link=link, usenet_password="np"))
        self.users.create(no_password, dict(record(), usenet_link=link))
        pool = Pool(Settings(env), env, self.store, self.users, clock=self.clock)
        try:
            usenet = pool.app(both).worker.usenet
            self.assertEqual((usenet.base, usenet.password), ("https://aios.test/stremio/n1/N1", "np"))
            self.assertIsNone(pool.app(no_password).worker.usenet)
        finally:
            pool.close()
        self.assertIsNone(self.pool.app(self.tokens[0]).worker.usenet)          # Usenet off on the instance

    def test_workers_share_one_turn(self):
        a, b = self.pool.app(self.tokens[0]), self.pool.app(self.tokens[1])
        self.assertIs(a.worker.turn, b.worker.turn)
        self.assertEqual(a.worker.turn.places, self.settings.syncs_at_once)
        self.assertIsNotNone(a.worker.limits)

    def test_closing_the_pool_closes_the_sources_ledger(self):
        # Pool.close stopped the workers and the lookup but left the built-in sources' ledger open: its
        # database (sources.sqlite3) and the source calls' pool outlived it
        import sqlite3
        from subsync.public import Pool
        env = dict(self.env, SUBSYNC_EXTRA_SOURCES="osv3")
        pool = Pool(Settings(env), env, self.store, self.users, clock=self.clock)
        ledger = pool.extra.ledger
        pool.close()
        with self.assertRaises(sqlite3.ProgrammingError):              # closed
            ledger.db.execute("SELECT 1")
        with self.assertRaises(RuntimeError):                          # no call starts after
            pool.extra.pool.submit(int)

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

    def test_a_worker_taking_or_running_a_job_is_not_reaped(self):
        # Reap read the worker's queue and running jobs without its cv, which a lane holds while it takes a
        # job off the queue and then marks it running: in between, a worker taking a job looked idle
        from subsync.worker import Job
        a = self.pool.app(self.tokens[0])
        self.clock.t += self.settings.worker_idle_s + 10
        job = Job("movie", "tt1", {"filename": "x.mkv"})
        for lane in ("current", "current_audio"):           # running in either lane
            with self.subTest(lane=lane):
                setattr(a.worker, lane, job)
                self.assertEqual(self.pool.reap(), 0)
                setattr(a.worker, lane, None)
        reaped = []
        with a.worker.cv:                                    # a lane taking its job: off the queue, not running yet
            reaping = threading.Thread(target=lambda: reaped.append(self.pool.reap()))
            reaping.start()
            reaping.join(0.3)                                # reap waits for the lane's step to end
            a.worker.current = job
        reaping.join(5)
        self.assertEqual(reaped, [0])
        a.worker.current = None
        self.assertEqual(self.pool.reap(), 1)                # idle now: stopped

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

    def test_a_persons_fair_share_key_leads_to_nothing_stored(self):
        # The sites' fair shares and the Usenet files not found counted people under the first half of their
        # record's id; now under a key of its own, made from their link (Users.who_key)
        from subsync.public import UserLimits
        limits = UserLimits(self.settings, self.users, self.tokens[0])
        self.assertEqual(limits.who, self.users.who_key(self.tokens[0]))
        self.assertNotIn(limits.who, self.users.user_id(self.tokens[0]))
        self.assertNotIn(limits.who, self.users.rows_key(self.tokens[0]))
        self.assertEqual(self.pool.app(self.tokens[0]).worker.who, limits.who)    # their worker counts them under it

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
        self.assertEqual(app.s.public_base, "https://subs.test/r/" + self.users.read_key(self.tokens[1]))
        self.assertEqual(app.s.languages, ["rus", "eng"])


SRT_EN = ("1\n00:00:01,000 --> 00:00:02,000\nHello there my friend\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n\n"
          "3\n00:00:05,000 --> 00:00:06,000\nYou are a bold one\n")
SYNCED_EN = SRT_EN.replace("00:00:01,000 --> 00:00:02,000", "00:00:01,500 --> 00:00:02,500")
SRT_FR = ("1\n00:00:01,000 --> 00:00:02,000\nBonjour mon ami\n\n2\n00:00:03,000 --> 00:00:04,000\nGénéral Kenobi\n\n"
          "3\n00:00:05,000 --> 00:00:06,000\nVous êtes bien audacieux\n")
FILE_KEY, ALIAS = "0123456789abcdef-1000", "hash:0123456789abcdef|1000"
QUERY = "videoHash=0123456789abcdef&videoSize=1000"     # the query string of a request for that file


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

    def close(self):   # as ExtraSources, when the pool closes: nothing held here
        pass


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
        return [e["label"] for e in app.subtitles("movie", "tt1", QUERY)]

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
        def pause_ends(safety):   # a refusal during the pause is the same one; the next in a row comes after
            safety.refused_until = safety.clock()
        token = self.person()
        app = self.pool.app(token)
        self.assertEqual(app.safety.refused(), 900)
        pause_ends(app.safety)
        self.assertEqual(app.safety.refused(), 1800)              # doubling with each refusal in a row
        self.clock.t += self.settings.worker_idle_s + 1
        self.assertEqual(self.pool.reap(), 1)
        again = self.pool.app(token)
        self.assertIs(again.safety, app.safety)
        pause_ends(again.safety)
        self.assertEqual(again.safety.refused(), 3600)
        self.pool.stop(token)                                     # a settings save keeps it
        self.assertIs(self.pool.app(token).safety, app.safety)
        self.pool.stop(token, reset=True)                         # a refresh: they may have changed their debrid key
        fresh = self.pool.app(token).safety
        self.assertIsNot(fresh, app.safety)
        self.assertEqual((fresh.refusals, fresh.backing_off()), (0, False))
        fresh.refused()
        pause_ends(fresh)
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


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class ListenTest(unittest.TestCase):
    def test_public_mode_listens_where_subsync_bind_says(self):
        # The one setting for both modes (the self-hosted one: test_service's StopTest)
        from unittest import mock
        from subsync import __main__ as entry, net, public, users
        from subsync.settings import Settings

        class Served(Exception):
            pass
        self.assertEqual(Settings({}).bind, "0.0.0.0")
        s = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PORT": "8791",
                      "SUBSYNC_BIND": " 127.0.0.1 "})
        with mock.patch.object(net, "configure"), mock.patch.object(entry, "public_store"), \
                mock.patch.object(users, "Users"), mock.patch.object(public, "Pool"), \
                mock.patch.object(public, "PublicService") as service, \
                mock.patch.object(public, "serve_public", side_effect=Served) as serve:
            with self.assertRaises(Served):
                entry.main_public(s)
        self.assertEqual(serve.call_args.args, (service.return_value, 8791, "127.0.0.1"))

    def test_public_mode_opens_the_peoples_records_setup_copies(self):
        # main_public named the people's database by a literal of its own, apart from what ./setup copies
        from unittest import mock
        from subsync import __main__ as entry, hostcmd, net, public, users
        from subsync.settings import Settings

        class Served(Exception):
            pass
        s = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY})
        with mock.patch.object(net, "configure"), mock.patch.object(entry, "public_store"), \
                mock.patch.object(users, "Users") as people, mock.patch.object(public, "Pool"), \
                mock.patch.object(public, "PublicService"), mock.patch.object(public, "serve_public", side_effect=Served):
            with self.assertRaises(Served):
                entry.main_public(s)
        self.assertEqual(people.call_args.args[0], os.path.join(s.data, hostcmd.USERS_DB))

    def test_public_mode_stops_on_sigterm_and_keeps_house_every_round(self):
        # main_public's SIGTERM handler and its housekeeping thread never ran in a test (the self-hosted
        # stop has test_service's StopTest)
        import signal
        import types
        from unittest import mock
        from subsync import __main__ as entry, net, public, users
        from subsync.settings import Settings
        handlers, threads, events = {}, [], []

        class Server:
            def serve_forever(self):
                events.append("serving")
                handlers[signal.SIGTERM](signal.SIGTERM, None)      # docker stop

            def shutdown(self):
                events.append("shut down")

        class Thread:
            def __init__(self, target, daemon=False, name=None):
                self.target, self.daemon, self.name = target, daemon, name
                threads.append(self)

            def start(self):
                if self.name != "subsync-housekeeping":           # the stop's shutdown, here at once
                    self.target()
        s = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY})
        with mock.patch.object(net, "configure"), mock.patch.object(entry, "public_store") as store, \
                mock.patch.object(users, "Users"), mock.patch.object(public, "Pool") as pool, \
                mock.patch.object(public, "PublicService"), \
                mock.patch.object(public, "serve_public", return_value=Server()), \
                mock.patch.object(entry, "threading", types.SimpleNamespace(Thread=Thread)), \
                mock.patch("signal.signal", lambda sig, handler: handlers.__setitem__(sig, handler)), \
                self.assertLogs("subsync", "INFO"):
            entry.main_public(s)
        pool.return_value.close.assert_called_once_with()               # every person's worker stops
        self.assertEqual(events, ["serving", "shut down"])
        self.assertTrue(all(t.daemon for t in threads))
        keeper = [t for t in threads if t.name == "subsync-housekeeping"]
        self.assertEqual(len(keeper), 1)
        sleeps, rounds = [], []

        class Done(Exception):
            pass

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise Done

        def housekeeping_round(*args):
            rounds.append(args)
            return args[-1]
        clock = iter((100.0, 160.0))
        with mock.patch.object(entry, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: next(clock))), \
                mock.patch.object(entry, "housekeeping_round", housekeeping_round), self.assertRaises(Done):
            keeper[0].target()
        self.assertEqual(sleeps, [entry.HOUSEKEEPING_EVERY_S] * 3)      # a round every HOUSEKEEPING_EVERY_S
        pool, store = pool.return_value, store.return_value
        self.assertEqual(rounds, [(pool, store, float("-inf"), 100.0), (pool, store, 100.0, 160.0)])


class StartupTest(unittest.TestCase):
    def run_main(self, env):
        """helpers.run_main's log lines at ERROR, as one text."""
        return "\n".join(run_main(self, env))

    def test_main_refuses_public_mode_without_a_secret(self):
        out = self.run_main({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        self.assertIn("SUBSYNC_SECRET_KEY", out)

    def test_main_refuses_a_setting_that_cant_work_with_one_plain_line(self):
        # A number written wrong raised out of Settings before main's checks, a traceback in a restart loop
        # ("ValueError: invalid literal for int with base 10: '6.0'");: these started
        own = {"SUBSYNC_SOURCE_MANIFEST": "https://aios.test/stremio/u/e/manifest.json", "SUBSYNC_SOURCE_PASSWORD": "p",
               "SUBSYNC_PUBLIC_BASE": "http://subsync.test:8790"}
        usenet_only = {"SUBSYNC_PUBLIC_BASE": "http://subsync.test:8790", "SUBSYNC_USENET": "on",
                       "SUBSYNC_USENET_MANIFEST": "https://aios.test/stremio/n/e/manifest.json",
                       "SUBSYNC_USENET_PASSWORD": "p", "SUBSYNC_USENET_LISTINGS_PER_DAY": "0"}
        public = {"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test", "SUBSYNC_SECRET_KEY": KEY}
        for env, line in (
                (dict(own, SUBSYNC_MAX_TRIES="6.0"), "SUBSYNC_MAX_TRIES must be a whole number from 1, like 6."),
                (dict(public, SUBSYNC_PORT="99999"), "SUBSYNC_PORT must be a whole number from 1 to 65535."),
                (dict(own, SUBSYNC_READ_TIMEOUT_S="0"), "SUBSYNC_READ_TIMEOUT_S must be a number above 0, like 60."),
                (dict(own, SUBSYNC_LANGUAGES="eng,xx"),
                 "SUBSYNC_LANGUAGES must be language codes separated by commas, like eng,rus: xx isn't one."),
                (dict(own, SUBSYNC_LANGUAGES=""), "SUBSYNC_LANGUAGES must name at least one language, like eng."),
                (dict(own, SUBSYNC_ADDON_KEY="a/b"), "SUBSYNC_ADDON_KEY can't contain /."),
                (usenet_only, "Usenet is this install's only source, so SUBSYNC_USENET_LISTINGS_PER_DAY must be a whole "
                              "number from 1."),
                (dict(own, SUBSYNC_USENET="on", SUBSYNC_USENET_MANIFEST="https://aios.test/stremio/n/e/manifest.json"),
                 "SUBSYNC_USENET_MANIFEST needs its password: set SUBSYNC_USENET_PASSWORD (./setup writes both).")):
            with self.subTest(env=env):
                out = self.run_main(env)
                self.assertEqual(out.splitlines(), [f"ERROR:subsync:not started: {line}"])

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
                                                      "reads_refused", "unreachable", "config_refused",
                                                      "aiostreams_errors"})
        for state, (title, text, action) in explain.PUBLIC_STATES.items():
            with self.subTest(state=state):
                self.assertFalse(title.endswith("."))
                self.assertNotIn("\n", text)
                if state == "config_refused":   # it ends with AIOStreams' own reason, quoted ('s words)
                    self.assertTrue(text.endswith(" AIOStreams' reason: \"{detail}\""), text[-30:])
                else:
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
        for rule in ("Public mode: your AIOStreams password is used once, unless you choose to keep it",
                     "Public mode: Real-Debrid is left out",
                     "Public mode: a few files at once", "Public mode: limits per person",
                     "Public mode: what is kept, and for how long"):
            with self.subTest(rule=rule):
                self.assertIn(rule, names)

    def test_the_settings_page_shows_small_reads_with_one_decimal(self):
        # A subtitle-track sync reads a few hundred KB: "0 MB read" after a sync would look like nothing was read.
        from subsync import pages
        s = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        record = {"languages": ["eng"]}
        # the limit with its thousands separator, as setup writes it
        for nbytes, shown in ((0, " 0 MB of 10,000 MB read"), (20 * 1024, " 0.1 MB of"), (351 * 1024, " 0.3 MB of"),
                              (5 * 1024 * 1024, " 5.0 MB of"), (12 * 1024 * 1024, " 12 MB of")):
            with self.subTest(nbytes=nbytes):
                html = pages.settings_page(s, record, {"files": 1, "bytes": nbytes}, "https://x.test/u/t/manifest.json")
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))      # the value and its unit are two spans
                self.assertRegex(text, r"(?<![\d.,])" + re.escape(shown.strip()))   # the number whole, not a tail

    def test_the_privacy_text_names_cloudflare_and_the_backups_before_how_long(self):
        # An instance with the owner's settings (SUBSYNC_FRONT_PROXY, SUBSYNC_BACKUP_DAYS) reads as before;
        # one without them, in words true of any host
        from subsync import explain
        from subsync.extra import asked_sources
        for env, added in (
                ({"SUBSYNC_FRONT_PROXY": "Cloudflare", "SUBSYNC_BACKUP_DAYS": "30"},
                 "AIOSubtitles runs behind Cloudflare, which passes every request on and so sees your internet address "
                 "and the links you open, your personal link included. The subtitle links your app gets don't carry "
                 "your personal link: their key opens only your subtitles, never your settings. AIOSubtitles itself "
                 "keeps no log of requests. Backups of the stored data are kept for up to 30 days."),
                ({}, "The web server in front of AIOSubtitles passes every request on and so sees your internet address "
                     "and the links you open, your personal link included; whoever hosts this instance runs it. The "
                     "subtitle links your app gets don't carry your personal link: their key opens only your "
                     "subtitles, never your settings. AIOSubtitles itself keeps no log of requests. Whoever hosts this "
                     "instance decides whether backups of the stored data are made and how long they are kept.")):
            with self.subTest(env=env):
                s = Settings(env)
                parts = explain.privacy_parts(s, asked_sources(s, "series"))
                text = " ".join(f"{head}: {text}" for head, text in parts)   # the parts in order, under their headings
                self.assertIn("Logs name the titles synced, never who asked. Who else sees your requests: " + added
                              + " How long it is kept:", text)

    def test_the_privacy_text_says_what_reading_a_track_of_pictures_means(self):
        # Image subtitle tracks' words are read on the server, and such a track can take a few MB
        from subsync import explain
        self.assertIn("for each file you play, its subtitle track (about 1 MB; a few MB when the track's lines are "
                      "pictures, whose words AIOSubtitles reads on its own server and doesn't keep) and the subtitles "
                      "your AIOStreams lists", dict(explain.privacy_parts(Settings({}), ()))["What is read"])

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
        # the audio part: the limit the person chooses, 1 GB its default
        self.assertIn("(about 1 MB per file, a few MB when the track's lines are pictures; up to the audio limit per "
                      "file with the audio route, 1 GB by default)", settings["SUBSYNC_DAILY_BUDGET_GB"])
        self.assertEqual(explain.SETTINGS["SUBSYNC_AUDIO_BUDGET_GB"][0], "1")
        self.assertIn("Reading a file's subtitle track takes about 1 MB, a few MB when the track's lines are "
                      "pictures; 10,000 is 200 files", settings["SUBSYNC_USER_DAILY_MB"])


if __name__ == "__main__":
    unittest.main()
