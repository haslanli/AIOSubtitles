"""Usenet support: the Usenet config, which services it reads through, and the rules that keep indexer
calls near zero. Plain Python; every run bounded (ulimit -t), no network beyond 127.0.0.1."""
import contextlib
import copy
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from helpers import (closed_after, Counts, DebridLink, FakeDebrid, FakeUsenet, HAVE_CRYPTOGRAPHY,
                     INSTANCE, KEY, NEEDS, PASSWORD, row)
from subsync import aiosconfig, realdebrid, sources, worker as worker_mod
from subsync.reader import RangeReader, ReadFailed
from subsync.settings import Settings
from subsync.store import day_of, Store
from subsync.worker import Job, Worker

URL = "https://aiosubtitles.example/u/TOKEN/manifest.json"
PROXIED = {"url": "http://nzbdav:3000", "apiKey": "k", "aiostreamsAuth": "user:pass"}
DIRECT = {"url": "http://nzbdav:3000", "apiKey": "k", "webdavUser": "u", "webdavPassword": "p"}


def viewer(services, presets, **extra):
    return {"services": copy.deepcopy(services), "presets": copy.deepcopy(presets), **extra}


TORBOX = {"id": "torbox", "enabled": True, "credentials": {"apiKey": "t"}}
ENGINE = {"id": "aiostreams", "enabled": True, "credentials": {"aiostreamsAuth": "a:b"}}
NZBDAV = {"id": "nzbdav", "enabled": True, "credentials": PROXIED}
NZBDAV_DIRECT = {"id": "nzbdav", "enabled": True, "credentials": DIRECT}
NNTP = {"id": "stremio_nntp", "enabled": True, "credentials": {"servers": "x"}}
EASYNEWS = {"id": "easynews", "enabled": True, "credentials": {"username": "u", "password": "p"}}
RD = {"id": "realdebrid", "enabled": True, "credentials": {"apiKey": "r"}}
NEWZNAB = {"type": "newznab", "instanceId": "nzb", "enabled": True, "options": {"name": "NZBGeek"}}
HYDRA = {"type": "nzbhydra", "instanceId": "hyd", "enabled": True, "options": {"name": "NZBHydra"}}
EN_SEARCH = {"type": "easynews-search", "instanceId": "ens", "enabled": True, "options": {"name": "Easynews Search"}}
PROWLARR = {"type": "prowlarr", "instanceId": "prw", "enabled": True, "options": {"name": "Prowlarr"}}
ZILEAN = {"type": "zilean", "instanceId": "zil", "enabled": True, "options": {"name": "Zilean"}}
TORRENTIO = {"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}}
STREAMER = {"type": "usenet-streamer", "instanceId": "ust", "enabled": True, "options": {"name": "UsenetStreamer"}}
EXTERNAL_EN = {"type": "easynewsPlus", "instanceId": "enp", "enabled": True, "options": {"name": "Easynews+"}}
TSUKI = {"type": "tsukihime", "instanceId": "tsk", "enabled": True, "options": {"name": "TsukiHime"}}
LIBRARY = {"type": "library", "instanceId": "lib", "enabled": True, "options": {"name": "Library"}}
SUBS = {"type": "opensubtitles-v3-plus", "instanceId": "osp", "enabled": True,
        "options": {"name": "OpenSubtitles V3+", "resources": ["subtitles"]}}
OWN = {"type": "custom", "instanceId": "ssy", "enabled": True,
       "options": {"name": "AIOSubtitles", "manifestUrl": URL, "resources": ["subtitles"]}}


def types(config):
    return [p["type"] for p in config["presets"]]


def ids(config):
    return [s["id"] for s in config["services"]]


class UsenetConfigTest(unittest.TestCase):
    """The Usenet config is the viewer's config with only its Usenet searches and the services that read
    part of a file, made by the sync config's own steps (fix A) and one more."""

    def make(self, v, **kw):
        return aiosconfig.usenet_sync_config(v, URL, names=("aiosubtitles",), **kw)

    def test_only_usenet_searches_and_readable_services(self):
        v = viewer([TORBOX, ENGINE, NZBDAV, NNTP, RD],
                   [ZILEAN, TORRENTIO, NEWZNAB, HYDRA, STREAMER, EXTERNAL_EN, TSUKI, LIBRARY, PROWLARR, SUBS, OWN])
        u = self.make(v)
        self.assertEqual(types(u), ["newznab", "nzbhydra", "prowlarr"])
        self.assertEqual(ids(u), ["torbox", "aiostreams", "nzbdav"])     # never Stremio NNTP, never Real-Debrid
        self.assertEqual(v["services"][3]["id"], "stremio_nntp")           # theirs as it was

    def test_prowlarr_searches_usenet_only(self):
        u = self.make(viewer([ENGINE], [PROWLARR]))
        self.assertEqual(u["presets"][0]["options"]["sources"], ["usenet"])
        torrent_only = dict(PROWLARR, options={"name": "Prowlarr", "sources": ["torrent"]})
        self.assertEqual(types(self.make(viewer([ENGINE], [NEWZNAB, torrent_only]))), ["newznab"])

    def test_nzbdav_and_altmount_only_through_aiostreams_proxy(self):
        # without AIOStreams' own proxy their links need the WebDAV password and often point into a home network
        u = self.make(viewer([NZBDAV_DIRECT, ENGINE], [NEWZNAB]))
        self.assertEqual(ids(u), ["aiostreams"])
        altmount = {"id": "altmount", "enabled": True, "credentials": {"url": "http://alt", "aiostreamsAuth": "x"}}
        self.assertEqual(ids(self.make(viewer([altmount], [NEWZNAB]))), ["altmount"])

    def test_fix_a_cuts_each_addons_own_services_choice(self):
        chosen = dict(NEWZNAB, options={"name": "NZBGeek", "services": ["stremio_nntp", "nzbdav"]})
        u = self.make(viewer([NNTP, NZBDAV, TORBOX], [chosen]))
        self.assertEqual(u["presets"][0]["options"]["services"], ["nzbdav"])
        nntp_only = dict(NEWZNAB, options={"name": "NZBGeek", "services": ["stremio_nntp"]})
        self.assertEqual(types(self.make(viewer([NNTP, NZBDAV], [nntp_only, HYDRA]))), ["nzbhydra"])

    def test_an_addon_with_no_service_it_can_use_is_left_out(self):
        # AIOStreams refuses Newznab, Prowlarr, Easynews Search… with no usable service, even with no choice of their own
        u = self.make(viewer([EASYNEWS], [NEWZNAB, EN_SEARCH, PROWLARR]))
        self.assertEqual(types(u), ["easynews-search"])
        self.assertEqual(types(self.make(viewer([ENGINE], [EN_SEARCH, NEWZNAB]))), ["newznab"])
        disabled = dict(ENGINE, enabled=False)
        self.assertEqual(types(self.make(viewer([disabled], [NEWZNAB]))), [])

    def test_no_background_work_uncached_left_out_errors_shown(self):
        v = viewer([ENGINE], [NEWZNAB], failover={"enabled": True}, preloadStreams={"enabled": True},
                   excludeUncachedFromServices=["torbox"], excludeUncachedFromAddons=["x"], excludeUncachedMode="and",
                   hideErrors=True, hideErrorsForResources=["stream", "catalog"], uuid="u", accessKey="a")
        u = self.make(v)
        for key, value in aiosconfig.SYNC_SETTINGS.items():
            if isinstance(value, dict):
                self.assertEqual({k: u[key][k] for k in value}, value)
            else:
                self.assertEqual(u[key], value)
        self.assertIs(u["excludeUncached"], True)
        for key in ("excludeUncachedFromServices", "excludeUncachedFromAddons", "excludeUncachedFromStreamTypes",
                    "excludeUncachedMode", "uuid", "accessKey"):
            self.assertNotIn(key, u)
        self.assertIs(u["hideErrors"], False)
        self.assertEqual(u["hideErrorsForResources"], ["catalog"])

    def test_subtitle_sources_only_when_asked(self):
        v = viewer([ENGINE], [NEWZNAB, SUBS, OWN])
        self.assertEqual(types(self.make(v)), ["newznab"])
        self.assertEqual(types(self.make(v, subtitles=True)), ["newznab", "opensubtitles-v3-plus"])   # never itself

    def test_no_way_to_real_debrid(self):
        v = viewer([RD, ENGINE], [NEWZNAB, dict(PROWLARR, options={"name": "P", "services": ["realdebrid", "aiostreams"]})])
        u = self.make(v)
        self.assertEqual(realdebrid.without_real_debrid(u)[1], [])
        self.assertEqual(u["presets"][1]["options"]["services"], ["aiostreams"])


class UsenetRouteTest(unittest.TestCase):
    """usenet_route: the services a config's Usenet files are read through, or why there are none."""

    def route(self, services, presets):
        return aiosconfig.usenet_route(viewer(services, presets), URL, names=("aiosubtitles",))

    def test_readable_services_in_a_fixed_order(self):
        self.assertEqual(self.route([NZBDAV, TORBOX, ENGINE], [NEWZNAB]), (["aiostreams", "nzbdav", "torbox"], None))
        self.assertEqual(self.route([EASYNEWS], [EN_SEARCH]), (["easynews"], None))

    def test_torbox_counts_only_with_a_usenet_search_that_can_use_it(self):
        self.assertEqual(self.route([TORBOX], [ZILEAN]), ([], "none"))
        self.assertEqual(self.route([TORBOX], [NEWZNAB]), (["torbox"], None))
        self.assertEqual(self.route([TORBOX, EASYNEWS], [EN_SEARCH]), (["easynews"], None))

    def test_why_none(self):
        self.assertEqual(self.route([NNTP, TORBOX], [ZILEAN, dict(NEWZNAB, options={"name": "N", "services": ["stremio_nntp"]})]),
                         ([], "nntp"))
        self.assertEqual(self.route([NZBDAV_DIRECT], [NEWZNAB]), ([], "proxy"))
        self.assertEqual(self.route([ENGINE], [STREAMER, EXTERNAL_EN]), ([], "none"))
        self.assertEqual(self.route([], []), ([], "none"))

    def test_names(self):
        self.assertEqual(aiosconfig.usenet_names(["aiostreams", "nzbdav", "torbox"]),
                         "AIOStreams' own Usenet streaming, NzbDAV and TorBox Usenet")
        self.assertEqual(aiosconfig.usenet_names(["easynews"]), "Easynews")


# ------------------------------------------------------------------------------------------- reading Usenet files

class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = Settings({})
        self.assertEqual((s.usenet, s.usenet_listings_per_day, s.usenet_manifest, s.usenet_password), (False, 10, "", ""))
        s = Settings({"SUBSYNC_USENET": "on", "SUBSYNC_USENET_LISTINGS_PER_DAY": "3"})
        self.assertEqual((s.usenet, s.usenet_listings_per_day), (True, 3))

    def test_a_usenet_only_install_needs_no_sync_config(self):
        base = {"SUBSYNC_PUBLIC_BASE": "http://x"}
        self.assertEqual(Settings(base).missing(), ["SUBSYNC_SOURCE_MANIFEST", "SUBSYNC_SOURCE_PASSWORD"])
        self.assertEqual(Settings(dict(base, SUBSYNC_USENET="on", SUBSYNC_USENET_MANIFEST="m",
                                       SUBSYNC_USENET_PASSWORD="p")).missing(), [])
        self.assertEqual(Settings(dict(base, SUBSYNC_USENET="off", SUBSYNC_USENET_MANIFEST="m",
                                       SUBSYNC_USENET_PASSWORD="p")).missing(),
                         ["SUBSYNC_SOURCE_MANIFEST", "SUBSYNC_SOURCE_PASSWORD"])

    def test_one_rule_says_whether_the_usenet_config_is_used(self):
        # The start, the self-hosted service and public mode each had their own rule
        usable = {"SUBSYNC_USENET": "on", "SUBSYNC_USENET_MANIFEST": "m", "SUBSYNC_USENET_PASSWORD": "p"}
        self.assertTrue(Settings(usable).usenet_usable)
        for name, value in (("SUBSYNC_USENET", "off"), ("SUBSYNC_USENET_MANIFEST", ""), ("SUBSYNC_USENET_PASSWORD", ""),
                            ("SUBSYNC_USENET_LISTINGS_PER_DAY", "0")):
            with self.subTest(name=name):
                self.assertFalse(Settings(dict(usable, **{name: value})).usenet_usable)

    def test_a_usenet_config_that_cant_be_used_stops_the_start(self):
        # A Usenet-only install with no search a day started and never found a file; a Usenet config without
        # its password was used anyway, every search blocked with a warning
        only = ("Usenet is this install's only source, so SUBSYNC_USENET_LISTINGS_PER_DAY must be a whole number "
                "from 1.")
        password = "SUBSYNC_USENET_MANIFEST needs its password: set SUBSYNC_USENET_PASSWORD (./setup writes both)."
        usenet = {"SUBSYNC_PUBLIC_BASE": "http://x", "SUBSYNC_USENET": "on", "SUBSYNC_USENET_MANIFEST": "m",
                  "SUBSYNC_USENET_PASSWORD": "p"}
        debrid = dict(usenet, SUBSYNC_SOURCE_MANIFEST="s", SUBSYNC_SOURCE_PASSWORD="q")
        for env, lines in ((dict(usenet, SUBSYNC_USENET_LISTINGS_PER_DAY="0"), [only]),
                           (dict(debrid, SUBSYNC_USENET_PASSWORD=""), [password]),
                           (dict(usenet, SUBSYNC_USENET_PASSWORD=""), [password]),
                           (dict(usenet, SUBSYNC_USENET_PASSWORD="", SUBSYNC_USENET_LISTINGS_PER_DAY="0"),
                            [password, only]),
                           (dict(debrid, SUBSYNC_USENET_LISTINGS_PER_DAY="0"), []),   # 0 turns Usenet off beside debrid
                           (usenet, []), (debrid, [])):
            with self.subTest(env=env):
                s = Settings(env)
                self.assertEqual((s.missing(), s.conflicts()), ([], lines))
        # a public instance's Usenet configs are each person's own
        self.assertEqual(Settings(dict(usenet, SUBSYNC_PUBLIC="on", SUBSYNC_USENET_PASSWORD="",
                                       SUBSYNC_USENET_LISTINGS_PER_DAY="0")).conflicts(), [])

    def test_no_new_cap_or_memory_setting(self):
        # Usenet reads take the index cap; the 24 h memory is a fixed rule
        with open(os.path.join(os.path.dirname(worker_mod.__file__), "settings.py"), encoding="utf-8") as f:
            settings = f.read()
        for name in ("SUBSYNC_USENET_FILE_MB", "SUBSYNC_USENET_UNLISTED_H"):
            self.assertNotIn(name, settings)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-usenet-")
        self.addCleanup(shutil.rmtree, self.dir)


class MemoryTest(TempDirTest):
    """A file not found on Usenet isn't looked up there again for 24 h, through restarts and idle workers."""

    def test_kept_in_the_store_for_a_day(self):
        now = [1_000_000.0]
        store = closed_after(self, Store(self.dir, clock=lambda: now[0]))
        store.usenet_missed("k1")
        self.assertTrue(store.usenet_recently_missed("k1", 86400))
        self.assertFalse(store.usenet_recently_missed("k2", 86400))
        again = closed_after(self, Store(self.dir, clock=lambda: now[0]))                 # after a restart
        self.assertTrue(again.usenet_recently_missed("k1", 86400))
        now[0] += 86401
        self.assertFalse(again.usenet_recently_missed("k1", 86400))

    def test_the_key_names_neither_person_nor_file(self):
        key = worker_mod.UsenetGate.key_of("who1", "name:Some.Film.2024.mkv|123")
        self.assertNotIn("Film", key)
        self.assertNotEqual(key, worker_mod.UsenetGate.key_of("who2", "name:Some.Film.2024.mkv|123"))

    def test_kept_two_days_also_when_no_new_one_comes(self):
        # Old notes went only when a new one was written, so once Usenet use stopped the last ones stayed
        # for good; the store's clean-up removes them now
        now = [1_000_000.0]
        store = closed_after(self, Store(self.dir, clock=lambda: now[0]))
        store.usenet_missed("old")
        now[0] += 86400
        store.usenet_missed("recent")
        now[0] += 86400 + 1                                    # "old" is past two days, and no new note came
        store.expire()
        self.assertEqual([k for (k,) in store.db.execute("SELECT key FROM usenet_misses")], ["recent"])

    def test_a_self_hosted_install_keeps_its_key_form(self):
        # Only public mode's notes moved under the person's own rows; a self-hosted install's keep their keys
        import hashlib
        store = closed_after(self, Store(self.dir))
        worker_mod.UsenetGate(store, 10, Counts()).missed("name:a.mkv|1")
        self.assertEqual([k for (k,) in store.db.execute("SELECT key FROM usenet_misses")],
                         [hashlib.sha256(b"usenet||name:a.mkv|1").hexdigest()[:32]])


class StoreCountsTest(TempDirTest):
    """A self-hosted service's Usenet listings and the day's stop, in the store's day counters."""

    def test_counted_per_day_and_stopped_until_the_next_one(self):
        now = [1_000_000.0]
        store = closed_after(self, Store(self.dir, clock=lambda: now[0]))
        counts = worker_mod.StoreUsenetCounts(store)
        self.assertEqual((counts.listings(), counts.stopped()), (0, False))
        counts.add_listing()
        counts.add_listing()
        counts.stop()
        counts.stop()                                                    # one stop a day
        self.assertEqual((counts.listings(), counts.stopped()), (2, True))
        self.assertEqual((store.day_count("usenet"), store.day_count("usenet_stop")), (2, 1))
        # after a restart
        again = worker_mod.StoreUsenetCounts(closed_after(self, Store(self.dir, clock=lambda: now[0])))
        self.assertEqual((again.listings(), again.stopped()), (2, True))
        now[0] += 86400
        self.assertEqual((counts.listings(), counts.stopped()), (0, False))


class GateTest(TempDirTest):
    """A listing is checked and counted in one step, so the day's last one is taken once, also when both lanes
    of a worker (a first pass and an audio job) ask for it at the same moment."""

    def test_the_days_last_listing_is_taken_once(self):
        class Slow(worker_mod.StoreUsenetCounts):
            def listings(self):
                got = super().listings()
                time.sleep(0.3)                             # the other lane asks meanwhile
                return got
        store = closed_after(self, Store(self.dir))
        counts = Slow(store)
        for _ in range(9):
            counts.add_listing()
        gate = worker_mod.UsenetGate(store, 10, counts, rows_key="w")
        taken = []
        lanes = [threading.Thread(target=lambda alias=alias: taken.append(gate.take(alias)), daemon=True)
                 for alias in ("name:a.mkv|1", "name:b.mkv|2")]
        for t in lanes:
            t.start()
        for t in lanes:
            t.join(5)
        self.assertEqual(sorted(taken), [False, True])
        self.assertEqual(store.day_count("usenet"), 10)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class UsageTest(TempDirTest):
    """Each person's Usenet listings and the day's stop, in a table of their own."""

    def users(self, now):
        from subsync.users import Users
        return Users(os.path.join(self.dir, "users.sqlite3"), b"s" * 32, clock=lambda: now[0])

    def test_counted_per_person_and_day(self):
        now = [1_000_000.0]
        users = closed_after(self, self.users(now))
        a, b = "a" * 43, "b" * 43
        users.create(a, {"x": 1})
        users.create(b, {"x": 1})
        self.assertEqual(users.usenet_usage(a), {"listings": 0, "stopped": False})
        users.add_usenet_listing(a)
        users.add_usenet_listing(a)
        users.stop_usenet(a)
        self.assertEqual(users.usenet_usage(a), {"listings": 2, "stopped": True})
        self.assertEqual(users.usenet_usage(b), {"listings": 0, "stopped": False})
        now[0] += 86400
        self.assertEqual(users.usenet_usage(a), {"listings": 0, "stopped": False})
        users.delete(a)
        db = sqlite3.connect(os.path.join(self.dir, "users.sqlite3"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM usenet_usage").fetchone()[0], 0)
        db.close()

    def test_a_persons_gate_counts_and_stops_their_own_day_only(self):
        # The gate Pool._build gives a person's worker counts through public.UserUsenetCounts, which no
        # test listed through
        from subsync.public import UserLimits
        now = [1_000_000.0]
        users = closed_after(self, self.users(now))
        store = closed_after(self, Store(self.dir))
        s = Settings({"SUBSYNC_DATA": self.dir})
        a, b = "a" * 43, "b" * 43
        users.create(a, {"x": 1})
        users.create(b, {"x": 1})
        gates = {}
        for token in (a, b):
            limits = UserLimits(s, users, token)
            gates[token] = worker_mod.UsenetGate(store, 2, limits.usenet_counts(), rows_key=limits.rows_key)
        self.assertEqual([gates[a].take(f"name:{n}.mkv|1") for n in "xyz"], [True, True, False])   # a's two a day
        self.assertEqual(users.usenet_usage(a), {"listings": 2, "stopped": False})
        self.assertEqual(users.usenet_usage(b), {"listings": 0, "stopped": False})
        with self.assertLogs("subsync.worker", "INFO"):
            gates[b].stop("the indexer's daily limit")
        self.assertEqual(users.usenet_usage(b), {"listings": 0, "stopped": True})
        self.assertFalse(gates[b].may_take("name:x.mkv|1"))
        self.assertEqual(users.usenet_usage(a), {"listings": 2, "stopped": False})     # a's day as it was
        now[0] += 86400
        self.assertTrue(gates[a].take("name:z.mkv|1") and gates[b].take("name:z.mkv|1"))   # a new UTC day

    def test_the_old_image_still_writes_its_usage(self):
        # the way back is the old image: its usage insert names no columns, so the usage table keeps its four, in the
        # order that insert gives them: a person's usage it writes is read back as such (the row, never
        # committed, was not read back, so columns in another order went unseen)
        now, token = [1_000_000.0], "a" * 43
        users = self.users(now)
        who = users.user_id(token)
        users.close()
        with contextlib.closing(sqlite3.connect(os.path.join(self.dir, "users.sqlite3"))) as db:
            db.execute("INSERT INTO usage VALUES (?,?,?,?) ON CONFLICT(id, day) DO UPDATE SET "
                       "files=files+excluded.files, bytes=bytes+excluded.bytes", (who, day_of(now[0]), 1, 2))
            db.commit()
        self.assertEqual(closed_after(self, self.users(now)).usage(token), {"files": 1, "bytes": 2})


class RowsTest(unittest.TestCase):
    """Which Usenet rows are read: through a readable service, ready, never a TorBox library item."""

    def test_usenet_rows_only_through_the_usenet_config(self):
        self.assertEqual(sources.unreadable(row()), "type usenet")
        self.assertIsNone(sources.unreadable(row(), usenet=True))

    def test_which(self):
        for service in ("aiostreams", "nzbdav", "altmount", "easynews", "torbox"):
            self.assertIsNone(sources.unreadable(row(service=service), usenet=True), service)
        self.assertEqual(sources.unreadable(row(service="stremio_nntp"), usenet=True), "usenet service stremio_nntp")
        self.assertEqual(sources.unreadable(row(service="realdebrid"), usenet=True), "a Real-Debrid file")
        self.assertEqual(sources.unreadable(row(service="torbox", cached=False), usenet=True), "not cached")
        self.assertEqual(sources.unreadable(row(service="torbox", library=True), usenet=True), "library row")
        self.assertIsNone(sources.unreadable(row(service="nzbdav", library=True), usenet=True))  # the played release
        self.assertEqual(sources.unreadable(row(url=None), usenet=True), "no url")
        self.assertEqual(sources.unreadable(row(kind="error"), usenet=True), "type error")

    def test_a_release_the_engine_opened_is_still_matched(self):
        # seen in a test: once AIOStreams' engine has opened a release (the viewer's own play), its listing
        # names the file inside (".mkv") at the file's size, while the player asked by the release's name at the NZB's
        # size
        a = sources.Aios("")
        asked = {"filename": "Breaking.Bad.S01E01.1080p.NF.WEB-DL.DDP5.1.x264-PiA", "videoSize": 4_097_690_000}
        opened = row(name="Breaking.Bad.S01E01.1080p.NF.WEB-DL.DDP5.1.x264-PiA.mkv", size=3_793_335_091, library=True)
        other = row(name="Breaking.Bad.S01E01.720p.NF.WEB-DL.DDP5.1.x264-PiA.mkv", size=2_400_000_000)
        self.assertIs(a.match([other, opened], asked, usenet=True), opened)
        self.assertIsNone(a.match([opened], asked))                     # debrid matching is unchanged
        # and the other way round: asked by the file, listed by the release
        listed = row(name="The.Matrix.1999.1080p.10bit.BluRay.6CH.x265.HEVC-PSA", size=1_753_362_000)
        self.assertIs(a.match([listed], {"filename": "The.Matrix.1999.1080p.10bit.BluRay.6CH.x265.HEVC-PSA.mkv",
                                         "videoSize": 1_665_140_622}, usenet=True), listed)
        # a far smaller file of the same name is another file (a size no video can have is left to the reader's rule)
        self.assertIsNone(a.match([row(name=asked["filename"] + ".mkv", size=1_000_000_000)], asked, usenet=True))
        exact = row(name=asked["filename"], size=asked["videoSize"])
        self.assertIs(a.match([opened, exact], asked, usenet=True), exact)

    def test_an_obfuscated_file_is_matched_by_its_release_folder(self):
        # seen in a test: a release whose file inside has a random name is listed, once opened, by that name,
        # with the release's name as the row's folder
        a = sources.Aios("")
        asked = {"filename": "The.Matrix.1999.1080p.10bit.BluRay.6CH.x265.HEVC-PSA", "videoSize": 1_753_362_000}
        opened = row(name="rNPhFFMoelkciQ8EpQuXmiUZ1H6sZ6vB.mkv", size=1_665_140_622, library=True)
        opened["streamData"]["folderName"] = "The.Matrix.1999.1080p.10bit.BluRay.6CH.x265.HEVC-PSA"
        other = row(name="ShVuo5EBlIdLczytNjxcHj6H6KraD08r.mkv", size=1_700_000_000, library=True)
        other["streamData"]["folderName"] = "Interstellar.2014.1080p.BluRay.x264-nikt0"
        self.assertIs(a.match([other, opened], asked, usenet=True), opened)

    def test_a_name_that_is_only_an_extension_names_no_release(self):
        # ".mkv" or blanks, without the extension, is "", which is also the name of every row's missing
        # folder: the first such row was matched and read, though it was another release
        a = sources.Aios("")
        listed = [row(name="Other.Movie.2019.1080p.mkv")]
        for name in (".mkv", "  ", " .MKV ", "\t.mp4\n"):
            with self.subTest(name=name):
                self.assertIsNone(sources.usenet_match(listed, name, None))
                self.assertIsNone(a.match(listed, {"filename": name}, usenet=True))
        self.assertEqual(sources.release_name(" Other.Movie.2019.1080p.mkv "), "Other.Movie.2019.1080p")
        self.assertIs(sources.usenet_match(listed, " Other.Movie.2019.1080p ", None), listed[0])

    def test_match_finds_the_played_file(self):
        a = sources.Aios("")
        listed = [row(name="b.mkv"), row(service="stremio_nntp"), row()]
        self.assertIs(a.match(listed, {"filename": "a.mkv", "videoSize": 2_000_000_000}, usenet=True), listed[2])
        self.assertIsNone(a.match(listed, {"filename": "a.mkv", "videoSize": 2_000_000_000}))


class ReaderTest(unittest.TestCase):
    """The reader: a Usenet file's link is asked at most twice per job, and its first read may take 120 s."""

    def setUp(self):
        self.d = DebridLink()
        self.addCleanup(self.d.close)

    def test_the_link_is_asked_at_most_twice(self):
        r = RangeReader(self.d.link, "test", max_re_resolve=1)
        r.get(0, 100)
        self.d.file_dead = True
        for k in range(10):
            with self.assertRaises(ReadFailed):
                r.get(100 * k, 50)
        self.assertLessEqual(self.d.link_hits, 2)

    def test_a_reader_given_the_address_an_earlier_one_resolved_reads_there_first(self):
        # The audio job's reader starts at the address its first pass's reader resolved, so the link
        # isn't asked (the Usenet service doesn't fetch the NZB again); when that address stops answering, the link is
        # asked again as after a redirect, at most twice in all
        first = RangeReader(self.d.link, "test", max_re_resolve=1)
        first.get(0, 100)
        self.assertEqual(self.d.link_hits, 1)
        later = RangeReader(self.d.link, "test", max_re_resolve=1, resolved=first.resolved)
        self.assertEqual((later.resolved, later.redirects), (first.resolved, True))
        self.assertEqual(later.get(0, 100), self.d.data[:100])
        self.assertEqual((self.d.link_hits, later.lookups), (1, 0))
        self.d.expire()
        self.assertEqual(later.get(100, 100), self.d.data[100:200])   # the link asked again: a new address
        self.assertEqual(self.d.link_hits, 2)
        self.d.file_dead = True
        for k in range(10):
            with self.assertRaises(ReadFailed):
                later.get(200 + 100 * k, 50)
        self.assertEqual(self.d.link_hits, 3)          # the first pass's look-up, then the audio job's two at most
        none = RangeReader(self.d.link, "test", resolved=None)    # nothing resolved before: as a new reader
        self.assertEqual((none.resolved, none.redirects), (None, False))

    def test_a_served_size_is_checked_by_a_rule_when_the_listed_one_is_not_the_files(self):
        # seen in a test: AIOStreams lists a Usenet release by its whole NZB (video, par2, nfo), so the file it
        # serves is a few per cent smaller; a reader with a size rule checks the served size by it instead of by an
        # exact size
        ok = RangeReader(self.d.link, "test", size_ok=lambda total: total == len(self.d.data))
        self.assertEqual(len(ok.get(0, 100)), 100)
        self.assertEqual(ok.size, len(self.d.data))
        refused = RangeReader(self.d.link, "test", size_ok=lambda total: False)
        with self.assertRaises(ReadFailed) as caught:
            refused.get(0, 100)
        self.assertIn("size differs", str(caught.exception))

    def test_the_usenet_size_rule(self):
        listed = 4_097_690_000                                   # Breaking Bad S01E01 as AIOStreams listed it
        fits = worker_mod.usenet_size_ok(listed)
        self.assertTrue(fits(3_793_335_091))                      # the file it served
        self.assertTrue(fits(listed))
        self.assertFalse(fits(19_856))                            # AIOStreams' download_failed.mp4
        self.assertFalse(fits(listed + 50_000_000))
        # nothing listed: the first reply decides, within the sizes a video file can have; AIOStreams' error video is
        # refused then too (it was read and indexed as the played file)
        unknown = worker_mod.usenet_size_ok(None)
        self.assertTrue(unknown(3_793_335_091))
        self.assertTrue(unknown(sources.MIN_VIDEO_BYTES))
        self.assertFalse(unknown(19_856))
        self.assertFalse(unknown(123))
        self.assertFalse(worker_mod.usenet_size_ok(15_000_000)(8_000_000))   # half the listed size, yet no video's

    def test_the_first_read_waits_longer(self):
        self.d.link_delay = 1.2
        r = RangeReader(self.d.link, "test", timeout=0.6, first_timeout=5)
        self.assertEqual(len(r.get(0, 100)), 100)
        self.d.expire()
        with self.assertRaises(ReadFailed):          # a later look-up has the plain timeout
            r.get(0, 100)


HINTS = {"filename": "a.mkv", "videoSize": 2_000_000_000}


class WorkerTest(TempDirTest):
    """Debrid first; then at most `per_day` Usenet listings, a 24 h memory, a stop for the day on any error."""

    def make(self, usenet, per_day=10, debrid=None, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng"}, **env))
        self.store = closed_after(self, Store(self.dir))
        self.counts = Counts()
        gate = worker_mod.UsenetGate(self.store, per_day, self.counts, rows_key="w")
        w = Worker(s, self.store, debrid or FakeDebrid(), worker_mod.Safety(), usenet=usenet, usenet_gate=gate)
        self.rows = []
        w._process_row = lambda job, hints, r, bt: self.rows.append((job.usenet, r))
        return w

    def test_debrid_first_then_one_usenet_listing(self):
        u = FakeUsenet([row()])
        w = self.make(u)
        w.process(Job("movie", "tt1", HINTS))
        self.assertEqual((u.listings, self.counts.n), (1, 1))
        self.assertEqual(self.rows, [(True, row())])

    def test_a_retry_of_the_same_file_doesnt_list_again(self):
        # a job stopped by the budget is queued again, and a pick can queue the file again: one listing for both
        u = FakeUsenet([row()])
        w = self.make(u)
        w.process(Job("movie", "tt1", HINTS))
        w.process(Job("movie", "tt1", HINTS))
        self.assertEqual((u.listings, len(self.rows)), (1, 2))

    def test_found_on_debrid_never_lists_usenet(self):
        class Has(FakeDebrid):
            def find_row(self, *a):
                return row(kind="debrid", service="torbox")
        u = FakeUsenet([row()])
        w = self.make(u, debrid=Has())
        w.process(Job("movie", "tt1", HINTS))
        self.assertEqual((u.listings, self.counts.n, self.rows[0][0]), (0, 0, False))

    def test_at_most_per_day_and_none_when_zero(self):
        u = FakeUsenet([])
        w = self.make(u, per_day=2)
        for i in range(5):
            w.process(Job("movie", f"tt{i}", dict(HINTS, filename=f"{i}.mkv")))
        self.assertEqual(u.listings, 2)
        u0 = FakeUsenet([row()])
        self.make(u0, per_day=0).process(Job("movie", "tt1", HINTS))
        self.assertEqual(u0.listings, 0)

    def test_not_found_isnt_looked_up_again_for_a_day(self):
        u = FakeUsenet([row(name="other.mkv")])
        w = self.make(u)
        w.process(Job("movie", "tt1", HINTS))
        w.unlisted.clear()                                   # the 1 h memory is gone (an idle worker stopped)
        w2 = self.make(u)                                    # a new worker, the same store
        w2.process(Job("movie", "tt1", HINTS))
        self.assertEqual(u.listings, 1)

    def test_an_error_row_or_a_failed_listing_stops_usenet_for_the_day(self):
        import urllib.error
        for fail, listed in [(None, [{"name": "[x] NZBGeek", "streamData": {"type": "error"}}, row()]),
                             (urllib.error.HTTPError("u", 429, "slow", {}, None), []),
                             (TimeoutError(), [])]:
            with self.subTest(fail=fail, listed=len(listed)):
                u = FakeUsenet(listed, fail=fail)
                w = self.make(u)
                w.process(Job("movie", "tt1", HINTS))
                self.assertTrue(self.counts.stopped_)
                self.assertEqual(self.rows, [])               # nothing read after an error
                w.process(Job("movie", "tt2", dict(HINTS, filename="b.mkv")))
                self.assertEqual(u.listings, 1)

    def test_a_config_that_cant_be_checked_spends_no_listing(self):
        # The config is checked before a listing is counted: one that can't be read lists nothing, and spends
        # none of the day's listings
        u = FakeUsenet([row()])
        u.blocked = True
        w = self.make(u)
        for i in range(12):
            w.process(Job("movie", f"tt{i}", dict(HINTS, filename=f"{i}.mkv")))
        self.assertEqual((u.config_checks, u.listings, self.counts.n, self.counts.stopped_), (12, 0, 0, False))
        u.blocked = False                                    # readable again: the next file is listed, and counted
        w.process(Job("movie", "tt1", HINTS))
        self.assertEqual((u.listings, self.counts.n), (1, 1))
        self.assertEqual(self.rows, [(True, row())])

    def test_a_cut_or_malformed_listing_stops_usenet_for_the_day(self):
        # As an HTTP error or no answer does, an answer cut short or one that isn't a listing stops Usenet
        import http.client

        class NotRows(FakeUsenet):
            def streams(self, *a, checked=False):
                self.listings += 1
                return ["not a row"]
        for u in (FakeUsenet([], fail=http.client.IncompleteRead(b"", 10)),           # no OSError
                  FakeUsenet([], fail=AttributeError("'list' object has no attribute 'get'")),   # not a JSON object
                  NotRows()):
            with self.subTest(usenet=u):
                w = self.make(u)
                w.process(Job("movie", "tt1", HINTS))
                self.assertTrue(self.counts.stopped_)
                self.assertEqual(self.rows, [])               # nothing read after an error
                w.process(Job("movie", "tt2", dict(HINTS, filename="b.mkv")))
                self.assertEqual(u.listings, 1)

    def test_a_file_that_backs_off_spends_no_usenet_listing(self):
        # Each request for a file that backs off queued a job that made a counted Usenet listing (at most one
        # per file an hour, within the day's limit), only to stop at the back-off. Its debrid listing stays, for a file
        # read lately under another name, and so does an audio job's Usenet listing
        u, debrid = FakeUsenet([row()]), FakeDebrid()
        w = self.make(u, debrid=debrid)
        job = Job("movie", "tt1", HINTS)
        for _ in range(4):
            self.store.file_failed(job.alias)                # after four failures: never read again
        with self.assertLogs("subsync.worker", "INFO") as logs:
            w.process(job)
        self.assertEqual((u.config_checks, u.listings, self.counts.n, self.rows), (0, 0, 0, []))
        self.assertEqual(debrid.calls, ["find_row"])
        self.assertIn(f"INFO:subsync.worker:file backing off after read failures {job.label}", logs.output)
        audio = Job("movie", "tt1", HINTS)
        audio.audio = audio.counted = True
        w.process(audio)
        self.assertEqual((u.listings, self.rows), (1, [(True, row())]))

    def test_no_usenet_without_a_usenet_config(self):
        w = self.make(None)
        w.process(Job("movie", "tt1", HINTS))
        self.assertEqual(self.rows, [])

    def test_the_subtitle_list_comes_through_the_usenet_config_without_a_sync_config(self):
        # a Usenet-only or Real-Debrid-with-Usenet setup has no sync config
        subs = [{"id": "s", "url": "http://subs/1.srt", "lang": "eng"}]
        u = FakeUsenet([], subs=subs)
        none = FakeDebrid()
        none.base = ""
        w = self.make(u, debrid=none)
        self.assertEqual([c["id"] for c in w._candidates(Job("movie", "tt1", HINTS), HINTS)], ["s"])
        self.assertEqual(u.calls, ["subtitles"])

    def test_the_worker_finds_through_one_finder_with_both_or_neither(self):
        # The Usenet config and its gate come together in the worker's finder, or Usenet is off
        u = FakeUsenet([row()])
        w = self.make(u)
        self.assertIs(w.usenet, u)
        self.assertIs(w.usenet_finder.config, u)
        for usenet, gate in ((u, None), (None, worker_mod.UsenetGate(self.store, 10, Counts()))):
            with self.subTest(usenet=usenet, gate=gate):
                alone = Worker(w.s, self.store, FakeDebrid(), worker_mod.Safety(), usenet=usenet, usenet_gate=gate)
                self.assertEqual((alone.usenet, alone.usenet_finder), (None, None))


class FinderTest(TempDirTest):
    """The Usenet finder alone: a found row is kept USENET_ROW_KEPT_S, with the address its first read
    resolved, for a retry and the audio job that follows; a file listed once isn't listed again meanwhile."""

    def finder(self, listed):
        self.store = closed_after(self, Store(self.dir))
        self.counts, self.usenet = Counts(), FakeUsenet(listed)
        return worker_mod.UsenetFinder(self.usenet, worker_mod.UsenetGate(self.store, 10, self.counts, rows_key="w"))

    def test_a_found_row_is_kept_with_its_address_for_the_audio_job(self):
        finder, job = self.finder([row()]), Job("movie", "tt1", HINTS)
        found = finder.find(job, HINTS)
        self.assertEqual(found, row())
        self.assertIs(finder.find(Job("movie", "tt1", HINTS), HINTS), found)      # a retry: nothing listed again
        self.assertEqual((self.usenet.listings, self.counts.n), (1, 1))
        finder.keep_address(job.alias, dict(found), "http://127.0.0.1:1/other")   # another row: nothing kept
        finder.keep_address(job.alias, found, None)                               # no address: nothing kept
        self.assertIsNone(finder.rows[job.alias][2])
        finder.keep_address(job.alias, found, "http://127.0.0.1:1/file")
        audio = Job("movie", "tt1", HINTS)
        self.assertIs(finder.audio_row(audio), found)
        self.assertEqual(audio.kept, (found, "http://127.0.0.1:1/file"))
        when, kept_row, address = finder.rows[job.alias]
        finder.rows[job.alias] = (when - worker_mod.USENET_ROW_KEPT_S, kept_row, address)   # the row's hour is over
        self.assertIs(finder.audio_row(audio), found)                             # the job keeps its own for its life
        self.assertIsNone(finder.audio_row(Job("movie", "tt1", HINTS)))
        self.assertEqual(finder.find(Job("movie", "tt1", HINTS), HINTS), row())   # listed again, counted
        self.assertEqual((self.usenet.listings, self.counts.n), (2, 2))

    def test_a_file_not_listed_is_missed_and_a_failed_read_stops_usenet(self):
        finder, job = self.finder([row(name="other.mkv")]), Job("movie", "tt1", HINTS)
        self.assertIsNone(finder.find(job, HINTS))
        self.assertEqual(finder.rows, {})
        self.assertIsNone(finder.find(job, HINTS))                                # not looked up again for a day
        self.assertEqual(self.usenet.listings, 1)
        other = Job("movie", "tt2", dict(HINTS, filename="b.mkv"))
        finder.missed(other.alias)
        self.assertTrue(self.store.usenet_recently_missed(finder.gate.key_of("w", other.alias),
                                                          worker_mod.USENET_UNLISTED_S))
        with self.assertLogs("subsync.worker", "INFO") as logs:
            finder.stop("the first read of a Usenet file failed")
        self.assertTrue(self.counts.stopped_)
        self.assertEqual(logs.output, ["INFO:subsync.worker:Usenet stopped until 00:00 UTC: the first read of a Usenet "
                                       "file failed"])


class UsenetJobTest(TempDirTest):
    """A Usenet file's first pass: the index cap, and a failed first read stops Usenet for the day; with no track it
    waits for a pick like a debrid file (its audio job: test_audio_choice.UsenetTest)."""

    def test_a_failed_first_read_stops_usenet_and_reads_index_only(self):
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_AUDIO_ROUTE": "on",
                      "SUBSYNC_AUDIO_WHEN": "open"})
        store = closed_after(self, Store(self.dir))
        counts = Counts()
        subs = [{"id": "s", "url": "http://127.0.0.1:1/1.srt", "lang": "eng"}]
        u = FakeUsenet([row(url="http://127.0.0.1:1/playback/1")], subs=subs)
        w = Worker(s, store, FakeDebrid(subs), worker_mod.Safety(), usenet=u,
                   usenet_gate=worker_mod.UsenetGate(store, 10, counts, rows_key="w"))
        job = Job("movie", "tt1", HINTS)
        self.assertFalse(w.audio_allowed(job))                   # a first pass reads the index only, opened or not
        seen = {}
        real = worker_mod.RangeReader

        def reader(*a, **k):
            seen.update(k)
            return real(*a, **k)
        worker_mod.RangeReader = reader
        self.addCleanup(setattr, worker_mod, "RangeReader", real)
        w.process(job)
        self.assertTrue(job.usenet)
        self.assertEqual(w._cap(job), s.index_cap)
        self.assertEqual((seen["max_re_resolve"], seen["first_timeout"]), (1, 120))
        self.assertIsNone(seen["size"])                          # a Usenet file's size is checked by the rule
        self.assertTrue(seen["size_ok"](HINTS["videoSize"] * 0.94))
        self.assertFalse(seen["size_ok"](19_856))
        self.assertTrue(counts.stopped_)


    def test_a_usenet_file_with_no_track_waits_for_a_pick(self):
        """A Usenet file with no usable track, once kept out of the audio route, now waits for the
        person's pick like a debrid file, and its languages aren't marked judged, so others can still pick it; the pick
        starts its audio job."""
        from unittest import mock
        from helpers import FIXTURES, RangeServer
        srv = RangeServer(FIXTURES)
        self.addCleanup(srv.close)
        # the listed and asked size is the served file's: the small fixture counts as a video file here, as a real one
        # would (a served size under sources.MIN_VIDEO_BYTES is no video's, worker.usenet_size_ok)
        patcher = mock.patch.object(sources, "MIN_VIDEO_BYTES", 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        size = os.path.getsize(os.path.join(FIXTURES, "tiny-nosub.mp4"))
        hints = {"filename": "tiny-nosub.mp4", "videoSize": size}
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_AUDIO_ROUTE": "on",
                      "SUBSYNC_EXTRA_SOURCES": ""})
        store = closed_after(self, Store(self.dir))
        subs = [{"id": "s", "url": srv.url("tiny.srt"), "lang": "eng", "release": "", "hash": "", "hash_match": False,
                 "ai": False}]
        u = FakeUsenet([row(url=srv.url("tiny-nosub.mp4"), name="tiny-nosub.mp4", size=size)], subs=subs)
        w = Worker(s, store, FakeDebrid(subs), worker_mod.Safety(), usenet=u,
                   usenet_gate=worker_mod.UsenetGate(store, 10, Counts(), rows_key="w"))
        job = Job("movie", "tt1", hints)
        w.process(job)
        self.assertTrue(job.usenet)
        file_key = store.resolve(job.alias)
        self.assertIsNotNone(file_key)                       # the file was read
        self.assertTrue(w.waits_for_pick(job.alias))
        self.assertIsNone(store.judged_all(file_key, ["eng"], s.rule_id, 0))
        self.assertTrue(w.pick(job.alias))
        self.assertEqual([(j.alias, j.audio) for j in w.jobs.values()], [(job.alias, True)])


# --------------------------------------------------------------------------------------------------------- public mode

OTHER = "https://other.test"
TORRENTIO_P = dict(TORRENTIO)


def setup_of(services, presets):
    return {"services": copy.deepcopy(services), "presets": copy.deepcopy(presets), "failover": {"enabled": True}}


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PublicUsenetTest(TempDirTest):
    def setUp(self):
        super().setUp()
        from helpers import FakeAiostreams
        from subsync import signup
        from subsync.aiosconfig import AiosApi
        from subsync.users import Users
        self.signup = signup
        self.s = self.settings()
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes())
        self.addCleanup(self.users.close)
        self.fake = FakeAiostreams()
        self.addCleanup(self.fake.close)
        self.other = FakeAiostreams()
        self.addCleanup(self.other.close)
        self.api = lambda base, timeout: AiosApi(base.replace(INSTANCE, self.fake.url).replace(OTHER, self.other.url),
                                                 timeout=timeout, max_timeout=timeout, retry_wait_cap=5)

    def settings(self, **env):
        return Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                              "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_USENET": "on"}, **env))

    def form(self, uuid, instance=INSTANCE, **extra):
        return dict({"link": f"{instance}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                     "languages": "English", "add_addon": "on", "usenet": "on"}, **extra)

    def run_signup(self, config, settings=None, **extra):
        uuid = self.fake.add(config, PASSWORD)
        return uuid, self.signup.signup(settings or self.s, self.users, self.form(uuid, **extra), api_factory=self.api,
                                        timeout=10)

    def config(self, uuid, fake=None):
        return (fake or self.fake).configs[uuid]["config"]

    def refresh(self, token, settings=None, **extra):
        form = {"password": PASSWORD, "add_addon": "on", "usenet": "on"}
        form.update(extra)
        return self.signup.refresh(settings or self.s, self.users, token, form, api_factory=self.api, timeout=10)

    # -- sign-up --------------------------------------------------------------------------------------------------
    def test_signup_with_usenet_makes_a_second_config(self):
        uuid, result = self.run_signup(setup_of([TORBOX, NZBDAV], [TORRENTIO, PROWLARR, NEWZNAB, SUBS]))
        self.assertTrue(result.usenet)
        record = self.users.open(result.token)
        sync = self.config(record["sync_uuid"])
        self.assertEqual(types(sync), ["torrentio", "prowlarr", "opensubtitles-v3-plus"])
        self.assertEqual(sync["presets"][1]["options"]["sources"], ["torrent"])   # Prowlarr: torrent indexers only
        usenet = self.config(record["usenet_uuid"])
        self.assertEqual(types(usenet), ["prowlarr", "newznab"])
        self.assertEqual(ids(usenet), ["torbox", "nzbdav"])
        self.assertTrue(record["usenet_link"].startswith(f"{INSTANCE}/stremio/{record['usenet_uuid']}/"))
        self.assertEqual((record["usenet"], record.get("usenet_why"), record["usenet_services"]),
                         (True, None, ["nzbdav", "torbox"]))
        self.assertEqual(len(self.fake.configs), 3)

    def test_a_refused_usenet_config_is_said_to_be_aiosubtitles_fault(self):
        answers = iter((None, "the Usenet config's reason"))     # the sync config is accepted, the Usenet one refused
        self.fake.refusal = lambda config: next(answers, None)
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(setup_of([TORBOX, NZBDAV], [TORRENTIO, PROWLARR, NEWZNAB, SUBS]))
        self.assertEqual(e.exception.message, (
            "Your AIOStreams didn't accept the config AIOSubtitles makes from yours. This is a fault in AIOSubtitles, "
            "not in your config, and your own config wasn't changed. AIOStreams' reason: \"the Usenet config's reason\""))
        self.assertEqual(len(self.fake.configs), 1)               # the sync config made a moment ago is gone again

    # -- nothing left behind when a later step meets a fault of AIOSubtitles' own -------------------------
    def faulty_creates(self, after):
        """The stand-in's client: `after` configs are made, then making one raises an error the client never gives."""
        made = [0]

        def factory(base, timeout):
            api = self.api(base, timeout)
            create = api.create_config

            def create_or_fail(config, password):
                if made[0] >= after:
                    raise RuntimeError("a fault")
                made[0] += 1
                return create(config, password)
            api.create_config = create_or_fail
            return api
        return factory

    def test_a_fault_making_the_usenet_config_removes_the_sync_config(self):
        uuid = self.fake.add(setup_of([TORBOX, NZBDAV], [TORRENTIO, PROWLARR, NEWZNAB, SUBS]), PASSWORD)
        with self.assertRaises(RuntimeError):
            self.signup.signup(self.s, self.users, self.form(uuid), api_factory=self.faulty_creates(1), timeout=10)
        self.assertEqual(list(self.fake.configs), [uuid])
        self.assertEqual(self.users.count(), 0)

    def test_a_fault_at_a_refresh_removes_what_it_made(self):
        uuid, result = self.run_signup(setup_of([TORBOX, NZBDAV], [TORRENTIO, PROWLARR, NEWZNAB, SUBS]), usenet="")
        before = self.users.open(result.token)
        del self.fake.configs[before["sync_uuid"]]                # gone: made again after the Usenet config, and fails
        with self.assertRaises(RuntimeError):
            self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD, "usenet": "on"},
                                api_factory=self.faulty_creates(1), timeout=10)
        self.assertEqual(list(self.fake.configs), [uuid])
        self.assertEqual(self.users.open(result.token), before)

    def test_a_fault_in_the_daily_rebuild_removes_the_usenet_config_it_made(self):
        uuid, result = self.run_signup(setup_of([TORBOX, NZBDAV], [TORRENTIO, PROWLARR, NEWZNAB, SUBS]), usenet="")
        record = self.users.open(result.token)
        record["usenet"] = True                                   # the box ticked since: the Usenet config is made
        del self.fake.configs[record["sync_uuid"]]                # first, then the sync config, which fails
        with self.assertRaises(RuntimeError):
            self.signup.rebuild(self.s, record, result.token, self.faulty_creates(1)(INSTANCE, 10), PASSWORD,
                                force=True)
        self.assertEqual(list(self.fake.configs), [uuid])

    def test_the_daily_rebuild_of_a_config_now_usenet_only_drops_its_sync_config(self):
        # Rebuild's branch for a config whose only service left reads Usenet had no test
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        sync = (record["sync_uuid"], record["sync_link"], record["sync_password"])
        self.fake.configs[uuid]["config"] = setup_of([ENGINE], [NEWZNAB, SUBS])    # TorBox removed: Usenet only now
        api = self.api(INSTANCE, 10)
        rebuilt = self.signup.rebuild(self.s, record, result.token, api, PASSWORD)
        self.assertIs(rebuilt.changed, True)
        self.assertEqual(rebuilt.drop, [sync])                         # the caller removes it once the record is saved
        self.assertNotIn("sync_uuid", record)
        self.assertIn(record["usenet_uuid"], self.fake.configs)
        rebuilt = self.signup.rebuild(self.s, record, result.token, api, PASSWORD)
        self.assertIs(rebuilt.changed, False)                          # nothing changed since
        self.assertEqual(rebuilt.drop, [])
        record["store_only"] = True                                    # a record for stored copies only, before
        self.assertIs(self.signup.rebuild(self.s, record, result.token, api, PASSWORD).changed, True)
        self.assertNotIn("store_only", record)                         # its Usenet config reads now
        self.assertFalse({"drop", "dropped"} & set(record))            # the record holds only its own fields

    def test_usenet_stays_off_unless_ticked_and_allowed(self):
        v = setup_of([TORBOX, NZBDAV], [TORRENTIO, NEWZNAB, SUBS])
        for settings, extra in [(self.s, {"usenet": ""}), (self.settings(SUBSYNC_USENET="off"), {})]:
            with self.subTest(allowed=settings.usenet):
                _, result = self.run_signup(v, settings=settings, **extra)
                record = self.users.open(result.token)
                self.assertFalse(result.usenet)
                self.assertNotIn("usenet_uuid", record)
                self.assertFalse(record.get("usenet"))

    def test_why_usenet_stays_off(self):
        _, result = self.run_signup(setup_of([TORBOX, NNTP], [TORRENTIO, dict(NEWZNAB, options={"name": "N", "services": ["stremio_nntp"]})]))
        self.assertEqual((result.usenet, result.usenet_why), (False, "nntp"))
        record = self.users.open(result.token)
        self.assertEqual((record["usenet"], record["usenet_why"]), (True, "nntp"))
        self.assertNotIn("usenet_uuid", record)

    def test_a_usenet_only_setup_signs_up_with_usenet_on(self):
        # no sync config; the Usenet config carries the subtitle sources
        _, result = self.run_signup(setup_of([ENGINE], [NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        self.assertNotIn("sync_uuid", record)
        self.assertFalse(result.store_only)
        self.assertEqual(types(self.config(record["usenet_uuid"])), ["newznab", "opensubtitles-v3-plus"])
        self.assertIs(record["usenet_subtitles"], True)

    def test_a_usenet_only_setup_stops_with_usenet_off(self):
        # With the box unticked it was told AIOSubtitles could read no Usenet service through it; the box is
        # the field it is about. Its daily rebuild says the same
        tick = 'Your config streams only through Usenet: tick "Sync Usenet files too" so AIOSubtitles can read its files.'
        for config in (setup_of([ENGINE], [NEWZNAB, SUBS]), setup_of([NZBDAV], [TORRENTIO, NEWZNAB, SUBS])):
            with self.subTest(services=ids(config)):
                with self.assertRaises(self.signup.SignupError) as e:
                    self.run_signup(config, usenet="")
                self.assertEqual((e.exception.message, e.exception.field, e.exception.unusable), (tick, "usenet", True))
        self.assertEqual(self.fake.writes(), [])
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]), usenet="")
        self.fake.configs[uuid]["config"]["services"] = [copy.deepcopy(ENGINE)]      # now Usenet only
        api = self.api(INSTANCE, 10)
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.rebuild(self.s, self.users.open(result.token), result.token, api, PASSWORD)
        self.assertEqual((e.exception.message, e.exception.field), (tick, "usenet"))

    def test_the_reason_names_usenet_only_where_people_can_turn_it_on(self):
        # On an instance that runs Usenet but offers no box (no searches a day), the reason still named Usenet
        # as something AIOSubtitles reads, and a Usenet-only config there has no box to tick
        debrid_only = ("AIOSubtitles can't find any file it could read through your AIOStreams: no debrid service "
                       "(AIOSubtitles reads files from debrid services only, not Usenet or torrents)")
        no_box = self.settings(SUBSYNC_USENET_LISTINGS_PER_DAY="0")
        for config, rest in ((setup_of([], [TORRENTIO, SUBS]), "."),
                             (setup_of([ENGINE], [NEWZNAB, SUBS]), "; no stream source other than Usenet ones.")):
            with self.subTest(services=ids(config)):
                with self.assertRaises(self.signup.SignupError) as e:
                    self.run_signup(config, settings=no_box, usenet="")
                self.assertEqual((e.exception.message, e.exception.field), (debrid_only + rest, None))
        with self.assertRaises(self.signup.SignupError) as e:       # the box offered: the reason names both
            self.run_signup(setup_of([], [TORRENTIO, SUBS]), usenet="")
        self.assertEqual((e.exception.message, e.exception.field), (
            "AIOSubtitles can't find any file it could read through your AIOStreams: no debrid or Usenet service "
            "AIOSubtitles can read (it reads ready debrid files, and Usenet files through services that stream part of "
            "a file; not torrents, not Stremio's NNTP streams).", None))

    def test_real_debrid_with_usenet_reads_usenet_only(self):
        _, result = self.run_signup(setup_of([RD, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        self.assertFalse(result.store_only)
        self.assertNotIn("store_only", record)
        usenet = self.config(record["usenet_uuid"])
        self.assertEqual((types(usenet), ids(usenet)), (["newznab", "opensubtitles-v3-plus"], ["aiostreams"]))
        _, off = self.run_signup(setup_of([RD, ENGINE], [TORRENTIO, NEWZNAB, SUBS]), usenet="")
        self.assertTrue(off.store_only)                                     # as before

    def test_a_refused_usenet_config_leaves_nothing_behind(self):
        self.fake.creates_left = 1
        before = len(self.fake.configs)
        with self.assertRaises(self.signup.SignupError):
            self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        self.assertEqual(len(self.fake.configs), before + 1)                # only the viewer's own config
        self.assertEqual(self.users.count(), 0)

    # -- refresh --------------------------------------------------------------------------------------------------
    def test_a_refresh_that_cant_make_the_usenet_config_leaves_the_sync_config_as_it_was(self):
        # Refresh saved into the sync config first and only then made the Usenet config, so when that failed
        # the sync config on AIOStreams had changed though the refresh had not; the Usenet config comes first now, as
        # in the daily rebuild
        uuid, result = self.run_signup(setup_of([TORBOX, NZBDAV], [TORRENTIO, PROWLARR, NEWZNAB, SUBS]), usenet="")
        record = self.users.open(result.token)
        self.assertNotIn("usenet_uuid", record)
        before = copy.deepcopy(self.config(record["sync_uuid"]))
        self.config(uuid)["presets"].append(dict(ZILEAN))                   # a change the sync config would follow
        self.fake.fault = "rate_limited"                                    # no new config on it this hour
        with self.assertRaises(self.signup.SignupError):
            self.refresh(result.token)                                      # the box ticked: a Usenet config is made
        self.assertEqual(self.config(record["sync_uuid"]), before)
        self.assertEqual(self.users.open(result.token), record)
        self.assertEqual(len(self.fake.configs), 2)                         # theirs and the sync config

    def test_refresh_turns_usenet_on_and_off(self):
        _, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]), usenet="")
        self.assertNotIn("usenet_uuid", self.users.open(result.token))
        self.refresh(result.token)
        record = self.users.open(result.token)
        made = record["usenet_uuid"]
        self.assertIn(made, self.fake.configs)
        self.refresh(result.token)                                           # saved again into the same one
        self.assertEqual(self.users.open(result.token)["usenet_uuid"], made)
        self.refresh(result.token, usenet="")
        record = self.users.open(result.token)
        self.assertNotIn(made, self.fake.configs)
        self.assertNotIn("usenet_uuid", record)
        self.assertFalse(record["usenet"])

    def test_refresh_moves_the_usenet_config_to_another_aiostreams(self):
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        old = self.users.open(result.token)
        new_main = self.other.add(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]), PASSWORD)
        self.refresh(result.token, link=f"{OTHER}/stremio/{new_main}/ENCRYPTED/manifest.json")
        record = self.users.open(result.token)
        self.assertNotIn(old["usenet_uuid"], self.fake.configs)
        self.assertNotIn(old["sync_uuid"], self.fake.configs)
        self.assertIn(record["usenet_uuid"], self.other.configs)

    def test_refresh_to_and_from_stored_copies(self):
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        first = self.users.open(result.token)
        self.fake.configs[uuid]["config"] = setup_of([RD, ENGINE], [TORRENTIO, NEWZNAB, SUBS])   # TorBox removed
        self.refresh(result.token)
        record = self.users.open(result.token)
        self.assertNotIn("sync_uuid", record)
        self.assertNotIn(first["sync_uuid"], self.fake.configs)
        self.assertTrue(record["usenet_subtitles"])
        self.assertEqual(types(self.config(record["usenet_uuid"])), ["newznab", "opensubtitles-v3-plus"])
        res = self.refresh(result.token, usenet="")                          # Usenet off: stored copies only
        record = self.users.open(result.token)
        self.assertTrue(res.store_only)
        self.assertTrue(record["store_only"])
        self.assertEqual(len(self.fake.configs), 1)

    def test_the_time_the_config_was_copied_is_kept_for_usenet_only_too(self):
        # A Usenet-only person's Refresh left no time (taking the sync config out took it), so the settings
        # page said "copied from yours at sign-up" right after it
        from subsync import pages
        now = [1_800_000_000.0]
        self.users.clock = lambda: now[0]
        _, result = self.run_signup(setup_of([ENGINE], [NEWZNAB, SUBS]))           # Usenet only from the start
        self.assertEqual(self.users.open(result.token)["synced_at"], now[0])
        now[0] += 5 * 86400
        self.refresh(result.token)
        record = self.users.open(result.token)
        self.assertEqual(record["synced_at"], now[0])
        self.assertEqual(pages.days_ago(record["synced_at"], now[0]), "today")
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        now[0] += 5 * 86400
        self.fake.configs[uuid]["config"] = setup_of([ENGINE], [NEWZNAB, SUBS])    # TorBox removed: Usenet only now
        self.refresh(result.token)
        record = self.users.open(result.token)
        self.assertNotIn("sync_uuid", record)
        self.assertEqual(record["synced_at"], now[0])

    # -- the kept password's rebuild and the daily check ----------------------------------------------------------
    def test_the_daily_check_rebuilds_and_repairs_the_usenet_config(self):
        from subsync.recheck import Checker
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]), keep_password="on")
        record = self.users.open(result.token)
        now = [time.time() + 2 * 86400]
        checker = Checker(self.s, self.users, api_factory=self.api, clock=lambda: now[0])
        del self.fake.configs[record["usenet_uuid"]]                        # gone: made again
        checker.check(result.token)
        again = self.users.open(result.token)
        self.assertIn(again["usenet_uuid"], self.fake.configs)
        self.assertNotEqual(again["usenet_uuid"], record["usenet_uuid"])
        self.fake.encrypted[again["usenet_uuid"]] = "NEWENC"                # its link changed: repaired
        now[0] += 2 * 86400
        checker.check(result.token)
        self.assertIn("/NEWENC/", self.users.open(result.token)["usenet_link"])
        self.fake.configs[uuid]["config"] = setup_of([TORBOX], [TORRENTIO, SUBS])   # no Usenet any more
        now[0] += 2 * 86400
        checker.check(result.token)
        record = self.users.open(result.token)
        self.assertNotIn("usenet_uuid", record)
        self.assertEqual(record["usenet_why"], "none")
        self.assertNotIn(again["usenet_uuid"], self.fake.configs)

    def test_a_rebuild_that_fails_leaves_no_new_usenet_config(self):
        from subsync.recheck import Checker
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]), keep_password="on")
        record = self.users.open(result.token)
        del self.fake.configs[record["usenet_uuid"]]                        # gone: the rebuild makes a new one
        self.fake.configs[uuid]["config"]["presets"].append(dict(ZILEAN))  # and the sync config changes
        self.fake.refuse_save_for.add(record["sync_uuid"])                  # whose save then fails
        checker = Checker(self.s, self.users, api_factory=self.api, clock=lambda: time.time() + 2 * 86400)
        self.assertIsNone(checker.check(result.token))      # an error answer (a 500) is counted, not reported
        self.assertEqual(self.users.open(result.token)["errors"], 1)
        self.assertEqual(sorted(self.fake.configs), sorted([uuid, record["sync_uuid"]]))

    def test_a_gone_usenet_config_without_a_kept_password_is_reported(self):
        from subsync.recheck import Checker
        _, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        del self.fake.configs[record["usenet_uuid"]]
        checker = Checker(self.s, self.users, api_factory=self.api, clock=lambda: time.time() + 2 * 86400)
        self.assertEqual(checker.check(result.token), "sync_gone")

    def test_delete_removes_both_configs(self):
        uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        outcome = self.signup.delete(self.s, self.users, result.token, {}, api_factory=self.api, timeout=10)
        self.assertEqual(list(self.fake.configs), [uuid])
        self.assertEqual(outcome, {"sync_removed": True, "usenet_removed": True, "addon_removed": False})

    def test_delete_records_which_config_it_couldnt_remove(self):
        # One flag for both configs, so the page said AIOSubtitles' own config with debrid keys was left when
        # it was the Usenet config, with Usenet and indexer keys
        from unittest import mock
        from subsync.aiosconfig import AiosError

        def failing(left):
            def factory(base, timeout):
                api = self.api(base, timeout)
                delete = api.delete_config

                def delete_config(uuid, password):
                    if uuid in left:
                        raise AiosError("Internal error", code="INTERNAL")
                    return delete(uuid, password)
                api.delete_config = mock.Mock(side_effect=delete_config)
                return api
            return factory
        for kinds in (("usenet",), ("sync",), ("sync", "usenet")):
            with self.subTest(left=kinds):
                uuid, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
                record = self.users.open(result.token)
                left = {record[f"{kind}_uuid"] for kind in kinds}
                outcome = self.signup.delete(self.s, self.users, result.token, {}, api_factory=failing(left),
                                             timeout=10)
                self.assertEqual(outcome, {"sync_removed": "sync" not in kinds, "usenet_removed": "usenet" not in kinds,
                                           "addon_removed": False})
                self.assertEqual(set(self.fake.configs) & {record["sync_uuid"], record["usenet_uuid"]}, left)
                self.assertIsNone(self.users.open(result.token))
        # Usenet only: no sync config of its own, so only the Usenet config can be left
        uuid, result = self.run_signup(setup_of([ENGINE], [NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        self.assertNotIn("sync_uuid", record)
        outcome = self.signup.delete(self.s, self.users, result.token, {}, api_factory=failing({record["usenet_uuid"]}),
                                     timeout=10)
        self.assertEqual(outcome, {"sync_removed": True, "usenet_removed": False, "addon_removed": False})

    def test_the_usenet_config_lists_through_its_own_link(self):
        # end to end through the stand-in: the Real-Debrid check before every request passes, and its rows are read
        _, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        self.fake.listings[record["usenet_uuid"]] = [row()]
        link = record["usenet_link"].replace(INSTANCE, self.fake.url)
        aios = sources.Aios(link, password=record["usenet_password"])
        self.assertEqual(aios.match(aios.streams("movie", "tt1"), HINTS, usenet=True), row())
        self.assertEqual(self.fake.listed, [(record["usenet_uuid"], "stream")])

    def test_one_usenet_listing_reads_its_config_once(self):
        # The finder checks the config before it counts a listing, and the listing (sources.Aios.streams)
        # read it again: two password-carrying calls to AIOStreams' user API for one request, the second able to
        # fail a listing already counted. The finder's check stands for its listing now
        _, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        self.fake.listings[record["usenet_uuid"]] = [row()]
        aios = sources.Aios(record["usenet_link"].replace(INSTANCE, self.fake.url), password=record["usenet_password"])
        counts = Counts()
        finder = worker_mod.UsenetFinder(aios, worker_mod.UsenetGate(closed_after(self, Store(self.dir)), 10, counts,
                                                                     rows_key="w"))
        del self.fake.requests[:]
        self.assertEqual(finder.find(Job("movie", "tt1", HINTS), HINTS), row())
        self.assertEqual(self.fake.requests.count(("GET", "/api/v1/user")), 1)
        self.assertEqual((self.fake.listed, counts.n), ([(record["usenet_uuid"], "stream")], 1))

    def test_a_persons_usenet_files_not_found_are_their_own_rows(self):
        # The notes of their Usenet files not found were kept under their fair-share key, outside their own
        # rows in the store, so Delete ("Everything AIOSubtitles keeps about you is deleted.") left them behind
        from subsync.public import Pool
        env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
               "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_USENET": "on", "SUBSYNC_EXTRA_SOURCES": "",
               "SUBSYNC_ORIGINAL_LANGUAGE": "off"}
        _, a = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        _, b = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        store = closed_after(self, Store(self.dir))
        pool = Pool(Settings(env), env, store, self.users, start_workers=False)
        self.addCleanup(pool.close)
        gates = {t: pool.app(t).worker.usenet_finder.gate for t in (a.token, b.token)}
        for gate in gates.values():
            gate.missed("name:a.mkv|1")
        mine = f"p:{self.users.rows_key(a.token)}:"
        key = worker_mod.UsenetGate.key_of(self.users.rows_key(a.token), "name:a.mkv|1")
        self.assertTrue(key.startswith(mine))
        self.assertTrue(store.usenet_recently_missed(key, 86400))
        self.assertFalse(gates[a.token].may_take("name:a.mkv|1"))           # not looked up again for a day
        self.assertEqual(store.drop_rows_of(self.users.rows_key(a.token)), 1)  # Delete's: their note goes
        self.assertTrue(gates[a.token].may_take("name:a.mkv|1"))
        self.assertFalse(gates[b.token].may_take("name:a.mkv|1"))           # anyone else's stays
        # once their data is deleted (the delete's stop), a job still running writes none of theirs
        pool.stop(a.token, forget=True)
        gates[a.token].missed("name:b.mkv|2")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM usenet_misses WHERE key LIKE ?",
                                          (mine + "%",)).fetchone()[0], 0)
        gates[b.token].missed("name:b.mkv|2")
        self.assertFalse(gates[b.token].may_take("name:b.mkv|2"))


class PagesTest(unittest.TestCase):
    """The public pages: the box only where the instance allows Usenet, the settings row's states, the gauge."""

    def settings(self, **env):
        return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_USENET": "on",
                              "SUBSYNC_USENET_LISTINGS_PER_DAY": "7"}, **env))

    def test_the_sign_up_box_only_where_usenet_is_allowed(self):
        from subsync import pages
        page = pages.signup_page(self.settings())
        self.assertIn('name="usenet"', page)
        self.assertIn("Sync Usenet files too", page)
        self.assertIn("at most 7 extra searches", page)
        self.assertNotIn('name="usenet"', pages.signup_page(self.settings(SUBSYNC_USENET="off")))

    def page(self, record, usenet_usage=None, error=None, field=None, **env):
        from subsync import pages
        base = {"languages": ["eng"], "sync_uuid": "s", "synced_at": 0}
        return pages.settings_page(self.settings(**env), dict(base, **record), {"files": 0, "bytes": 0},
                                   "https://subs.test/u/T/manifest.json", now=0, error=error, field=field,
                                   usenet_usage=usenet_usage or {"listings": 2, "stopped": False})

    def test_a_problem_with_the_usenet_box_marks_it(self):
        # "tick Sync Usenet files too" links to the box and marks it, on the sign-up and the settings page
        from helpers import Tree
        from subsync import pages
        for text in (pages.signup_page(self.settings(), error="Tick it.", field="usenet"),
                     self.page({}, error="Tick it.", field="usenet")):
            tree = Tree(text)
            box = tree.all("input", id="usenet")
            self.assertEqual([(b.get("aria-invalid"), b["aria-describedby"]) for b in box],
                             [("true", "problem-message usenet-help")])
            self.assertEqual(tree.all("a", href="#usenet")[0].get("href"), "#usenet")
        for text in (pages.signup_page(self.settings()), self.page({}), self.page({}, error="Wrong.", field="refresh")):
            self.assertNotIn("aria-invalid", Tree(text).all("input", id="usenet")[0])

    def test_the_settings_row(self):
        on = {"usenet": True, "usenet_uuid": "u", "usenet_services": ["nzbdav", "torbox"]}
        page = self.page(on)
        self.assertIn("Usenet: on, through NzbDAV and TorBox Usenet (2 of 7 searches used today)", page)
        self.assertIn("searches used today", page)
        self.assertRegex(page, r'name="usenet"[^>]*checked')
        self.assertIn("Usenet is paused until 00:00 UTC", self.page(on, {"listings": 3, "stopped": True}))
        off = self.page({"usenet": True, "usenet_why": "proxy"})
        self.assertIn("Usenet: off", off)
        self.assertIn("Turn on AIOStreams&#x27; proxy", off)
        self.assertNotIn("searches used today", off)
        hidden = self.page(on, SUBSYNC_USENET="off")
        self.assertNotIn('id="usenet-h"', hidden)
        self.assertNotIn('name="usenet"', hidden)
        self.assertNotIn("searches used today", hidden)

    def test_the_signed_up_page(self):
        from helpers import result                 # the stand-in checked against signup.SignupResult
        from subsync import explain, pages

        def done(**kw):
            return result(changes=[], languages=["eng"], **kw)
        made = pages.result_page(done(usenet=True))
        self.assertIn("Usenet is on: AIOSubtitles made a second config on your AIOStreams with only your Usenet "
                      "searches and services.", made)
        self.assertIn("Made AIOSubtitles&#x27; own config", made)
        only = pages.result_page(done(usenet=True, sync_made=False))
        self.assertNotIn("Made AIOSubtitles&#x27; own config", only)
        # It spoke of "a second config" with only Usenet in it; it is the only one, with the subtitle sources
        self.assertIn("Usenet is on: AIOSubtitles made its own config on your AIOStreams with only your Usenet "
                      "searches, Usenet services and subtitle sources.", only)
        self.assertNotIn("second config", only)
        why = pages.result_page(done(usenet_why="nntp"))
        self.assertIn("Usenet stays off: your AIOStreams has no Usenet service", why)
        self.assertEqual(explain.USENET_WHY["none"], explain.USENET_WHY["nntp"])


if __name__ == "__main__":
    unittest.main()
