"""Usenet support: the Usenet config, which services it reads through, and the rules that keep indexer
calls near zero. Plain Python; every run bounded (ulimit -t), no network beyond 127.0.0.1."""
import copy
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_resolve_once import Debrid  # noqa: E402
from subsync import aiosconfig, realdebrid, sources, worker as worker_mod  # noqa: E402
from subsync.reader import RangeReader, ReadFailed  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.store import Store  # noqa: E402
from subsync.worker import Job, Worker  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False

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

    def test_no_new_cap_or_memory_setting(self):
        # Usenet reads take the index cap; the 24 h memory is a fixed rule
        for name in ("SUBSYNC_USENET_FILE_MB", "SUBSYNC_USENET_UNLISTED_H"):
            self.assertNotIn(name, open(os.path.join(os.path.dirname(worker_mod.__file__), "settings.py")).read())


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-usenet-")
        self.addCleanup(shutil.rmtree, self.dir)


class MemoryTest(TempDirTest):
    """A file not found on Usenet isn't looked up there again for 24 h, through restarts and idle workers."""

    def test_kept_in_the_store_for_a_day(self):
        now = [1_000_000.0]
        store = Store(self.dir, clock=lambda: now[0])
        store.usenet_missed("k1")
        self.assertTrue(store.usenet_recently_missed("k1", 86400))
        self.assertFalse(store.usenet_recently_missed("k2", 86400))
        again = Store(self.dir, clock=lambda: now[0])                 # after a restart
        self.assertTrue(again.usenet_recently_missed("k1", 86400))
        now[0] += 86401
        self.assertFalse(again.usenet_recently_missed("k1", 86400))

    def test_the_key_names_neither_person_nor_file(self):
        key = worker_mod.UsenetGate.key_of("who1", "name:Some.Film.2024.mkv|123")
        self.assertNotIn("Film", key)
        self.assertNotEqual(key, worker_mod.UsenetGate.key_of("who2", "name:Some.Film.2024.mkv|123"))


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class UsageTest(TempDirTest):
    """Each person's Usenet listings and the day's stop, in a table of their own."""

    def users(self, now):
        from subsync.users import Users
        return Users(os.path.join(self.dir, "users.sqlite3"), b"s" * 32, clock=lambda: now[0])

    def test_counted_per_person_and_day(self):
        now = [1_000_000.0]
        users = self.users(now)
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

    def test_the_old_image_still_writes_its_usage(self):
        # the way back is the old image: its usage insert names no columns, so the usage table keeps its four
        self.users([1_000_000.0]).close()
        db = sqlite3.connect(os.path.join(self.dir, "users.sqlite3"))
        db.execute("INSERT INTO usage VALUES (?,?,?,?) ON CONFLICT(id, day) DO UPDATE SET files=files+excluded.files, "
                   "bytes=bytes+excluded.bytes", ("id", "2026-11-13", 1, 2))
        db.close()


def row(kind="usenet", service="aiostreams", cached=True, library=False, url="http://aios/playback/1", name="a.mkv",
        size=2_000_000_000):
    return {"url": url, "streamData": {"type": kind, "filename": name, "size": size, "library": library,
                                        "service": {"id": service, "cached": cached}}}


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
        #'s test: once AIOStreams' engine has opened a release (the viewer's own play), its listing names the file
        # inside (".mkv") at the file's size, while the player asked by the release's name at the NZB's size
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
        #'s test: a release whose file inside has a random name is listed, once opened, by that name, with the
        # release's name as the row's folder
        a = sources.Aios("")
        asked = {"filename": "The.Matrix.1999.1080p.10bit.BluRay.6CH.x265.HEVC-PSA", "videoSize": 1_753_362_000}
        opened = row(name="rNPhFFMoelkciQ8EpQuXmiUZ1H6sZ6vB.mkv", size=1_665_140_622, library=True)
        opened["streamData"]["folderName"] = "The.Matrix.1999.1080p.10bit.BluRay.6CH.x265.HEVC-PSA"
        other = row(name="ShVuo5EBlIdLczytNjxcHj6H6KraD08r.mkv", size=1_700_000_000, library=True)
        other["streamData"]["folderName"] = "Interstellar.2014.1080p.BluRay.x264-nikt0"
        self.assertIs(a.match([other, opened], asked, usenet=True), opened)

    def test_match_finds_the_played_file(self):
        a = sources.Aios("")
        listed = [row(name="b.mkv"), row(service="stremio_nntp"), row()]
        self.assertIs(a.match(listed, {"filename": "a.mkv", "videoSize": 2_000_000_000}, usenet=True), listed[2])
        self.assertIsNone(a.match(listed, {"filename": "a.mkv", "videoSize": 2_000_000_000}))


class ReaderTest(unittest.TestCase):
    """The reader: a Usenet file's link is asked at most twice per job, and its first read may take 120 s."""

    def setUp(self):
        self.d = Debrid()
        self.addCleanup(self.d.close)

    def test_the_link_is_asked_at_most_twice(self):
        r = RangeReader(self.d.link, "test", max_re_resolve=1)
        r.get(0, 100)
        self.d.file_dead = True
        for k in range(10):
            with self.assertRaises(ReadFailed):
                r.get(100 * k, 50)
        self.assertLessEqual(self.d.link_hits, 2)

    def test_a_served_size_is_checked_by_a_rule_when_the_listed_one_is_not_the_files(self):
        #'s test: AIOStreams lists a Usenet release by its whole NZB (video, par2, nfo), so the file it serves is
        # a few per cent smaller; a reader with a size rule checks the served size by it instead of by an exact size
        from test_resolve_once import DATA
        ok = RangeReader(self.d.link, "test", size_ok=lambda total: total == len(DATA))
        self.assertEqual(len(ok.get(0, 100)), 100)
        self.assertEqual(ok.size, len(DATA))
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
        self.assertTrue(worker_mod.usenet_size_ok(None)(123))     # nothing listed: the first reply decides

    def test_the_first_read_waits_longer(self):
        self.d.link_delay = 1.2
        r = RangeReader(self.d.link, "test", timeout=0.6, first_timeout=5)
        self.assertEqual(len(r.get(0, 100)), 100)
        self.d.expire()
        with self.assertRaises(ReadFailed):          # a later look-up has the plain timeout
            r.get(0, 100)


class FakeDebrid:
    """The sync config: never has the file (it is on Usenet only)."""

    def __init__(self, subs=()):
        self.base, self.subs, self.config_sources, self.calls = "https://aios/stremio/d/e", list(subs), set(), []

    def find_row(self, *a):
        self.calls.append("find_row")
        return None

    def subtitles(self, *a):
        self.calls.append("subtitles")
        return [dict(c) for c in self.subs]


class FakeUsenet(FakeDebrid):
    def __init__(self, listed=(), fail=None, subs=()):
        super().__init__(subs)
        self.base = "https://aios/stremio/u/e"
        self.listed, self.fail, self.listings = list(listed), fail, 0

    def streams(self, *a):
        self.listings += 1
        if self.fail:
            raise self.fail
        return [dict(r) for r in self.listed]

    def match(self, listed, hints, usenet=False):
        return sources.Aios("").match(listed, hints, usenet=usenet)


class Counts:
    def __init__(self):
        self.n, self.stopped_ = 0, False

    def listings(self):
        return self.n

    def add_listing(self):
        self.n += 1

    def stopped(self):
        return self.stopped_

    def stop(self):
        self.stopped_ = True


HINTS = {"filename": "a.mkv", "videoSize": 2_000_000_000}


class WorkerTest(TempDirTest):
    """Debrid first; then at most `per_day` Usenet listings, a 24 h memory, a stop for the day on any error."""

    def make(self, usenet, per_day=10, debrid=None, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng"}, **env))
        self.store = Store(self.dir)
        self.counts = Counts()
        gate = worker_mod.UsenetGate(self.store, per_day, self.counts, who="w")
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


class UsenetJobTest(TempDirTest):
    """A Usenet job: the index cap, never the audio route, and a failed first read stops Usenet for the day."""

    def test_a_failed_first_read_stops_usenet_and_reads_index_only(self):
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_AUDIO_ROUTE": "on",
                      "SUBSYNC_AUDIO_ON_PICK": "off"})
        store = Store(self.dir)
        counts = Counts()
        subs = [{"id": "s", "url": "http://127.0.0.1:1/1.srt", "lang": "eng"}]
        u = FakeUsenet([row(url="http://127.0.0.1:1/playback/1")], subs=subs)
        w = Worker(s, store, FakeDebrid(subs), worker_mod.Safety(), usenet=u,
                   usenet_gate=worker_mod.UsenetGate(store, 10, counts, who="w"))
        job = Job("movie", "tt1", HINTS)
        self.assertTrue(w.audio_allowed(job))
        seen = {}
        real = worker_mod.RangeReader

        def reader(*a, **k):
            seen.update(k)
            return real(*a, **k)
        worker_mod.RangeReader = reader
        self.addCleanup(setattr, worker_mod, "RangeReader", real)
        w.process(job)
        self.assertTrue(job.usenet)
        self.assertFalse(w.audio_allowed(job))
        self.assertEqual((seen["max_re_resolve"], seen["first_timeout"]), (1, 120))
        self.assertIsNone(seen["size"])                          # a Usenet file's size is checked by the rule
        self.assertTrue(seen["size_ok"](HINTS["videoSize"] * 0.94))
        self.assertFalse(seen["size_ok"](19_856))
        self.assertTrue(counts.stopped_)



# --------------------------------------------------------------------------------------------------------- public mode

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
INSTANCE = "https://aios.test"
OTHER = "https://other.test"
PASSWORD = "the-viewers-own-Password-1"
TORRENTIO_P = dict(TORRENTIO)


def setup_of(services, presets):
    return {"services": copy.deepcopy(services), "presets": copy.deepcopy(presets), "failover": {"enabled": True}}


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
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

    def refresh(self, token, settings=None, fake=None, **extra):
        uuid = self.users.open(token)["main_uuid"]
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
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(setup_of([ENGINE], [NEWZNAB, SUBS]), usenet="")
        self.assertIn("Usenet", e.exception.message)
        self.assertEqual(self.fake.writes(), [])

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
        self.assertEqual(checker.check(result.token), "cannot_rebuild")
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
        self.signup.delete(self.s, self.users, result.token, {}, api_factory=self.api, timeout=10)
        self.assertEqual(list(self.fake.configs), [uuid])

    def test_the_usenet_config_lists_through_its_own_link(self):
        # end to end through the stand-in: the Real-Debrid check before every request passes, and its rows are read
        _, result = self.run_signup(setup_of([TORBOX, ENGINE], [TORRENTIO, NEWZNAB, SUBS]))
        record = self.users.open(result.token)
        self.fake.listings[record["usenet_uuid"]] = [row()]
        link = record["usenet_link"].replace(INSTANCE, self.fake.url)
        aios = sources.Aios(link, password=record["usenet_password"])
        self.assertEqual(aios.match(aios.streams("movie", "tt1"), HINTS, usenet=True), row())
        self.assertEqual(self.fake.listed, [(record["usenet_uuid"], "stream")])



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

    def page(self, record, usenet_usage=None, **env):
        from subsync import pages
        base = {"languages": ["eng"], "sync_uuid": "s", "synced_at": 0}
        return pages.settings_page(self.settings(**env), dict(base, **record), {"files": 0, "bytes": 0},
                                   "https://subs.test/u/T/manifest.json", now=0,
                                   usenet_usage=usenet_usage or {"listings": 2, "stopped": False})

    def test_the_settings_row(self):
        on = {"usenet": True, "usenet_uuid": "u", "usenet_services": ["nzbdav", "torbox"]}
        page = self.page(on)
        self.assertIn("Usenet: on, through NzbDAV and TorBox Usenet (2 of 7 searches used today)", page)
        self.assertIn("searches used today", page)
        self.assertRegex(page, r'name="usenet"[^>]*checked')
        self.assertIn("Usenet is paused until 00:00 UTC", self.page(on, {"listings": 3, "stopped": True}))
        off = self.page({"usenet": True, "usenet_why": "proxy"})
        self.assertIn("Usenet: off", off)
        self.assertIn("Turn on AIOStreams&#x27;s proxy", off.replace("AIOStreams&#x27; proxy", "AIOStreams&#x27;s proxy"))
        self.assertNotIn("searches used today", off)
        hidden = self.page(on, SUBSYNC_USENET="off")
        self.assertNotIn('id="usenet-h"', hidden)
        self.assertNotIn('name="usenet"', hidden)
        self.assertNotIn("searches used today", hidden)

    def test_the_signed_up_page(self):
        import types
        from subsync import explain, pages

        def SignupResult(token, manifest_url, languages=(), **kw):
            return types.SimpleNamespace(token=token, manifest_url=manifest_url, languages=list(languages), changes=[],
                                         warning=None, added=True, manual=False, **kw)
        made = pages.result_page(SignupResult("T", "https://subs.test/u/T/manifest.json", languages=["eng"], usenet=True))
        self.assertIn("Usenet is on: AIOSubtitles made a second config", made)
        self.assertIn("Made AIOSubtitles&#x27; own config", made)
        only = pages.result_page(SignupResult("T", "https://subs.test/u/T/manifest.json", languages=["eng"], usenet=True,
                                              sync_made=False))
        self.assertNotIn("Made AIOSubtitles&#x27; own config", only)
        why = pages.result_page(SignupResult("T", "https://subs.test/u/T/manifest.json", languages=["eng"],
                                             usenet_why="nntp"))
        self.assertIn("Usenet stays off: your AIOStreams has no Usenet service", why)
        self.assertEqual(explain.USENET_WHY["none"], explain.USENET_WHY["nntp"])


if __name__ == "__main__":
    unittest.main()
