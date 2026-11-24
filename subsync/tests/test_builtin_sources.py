"""The subtitle sources AIOSubtitles asks itself. A local stand-in answers like TheSubtitleDB (by IMDb id,
torrent hash and release name), Stremio's OpenSubtitles v3, Cinemeta + Gestdown and SubSource; no network (a test that
opens a connection to anything but this machine fails)."""
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
from unittest import mock

from helpers import Clock, FakeSources, HASH, PACK, SINGLE, SourcesHarness, wait_until
from subsync import sourcestate, srclimits
from subsync.extra import Busy, split_id
from subsync.settings import Settings
from subsync.sourcestate import Health, Ledger
from subsync.sources import fetch_subtitle


def closed(error):
    """An HTTP error closed, as ExtraSources._get raises one."""
    error.close()
    return error


class Ticking:
    """A clock that gives `times` one reading after another, then stays at the last."""

    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]


class IdsTest(SourcesHarness, unittest.TestCase):
    def test_only_imdb_ids_are_sent_as_imdb_ids(self):
        self.assertEqual(split_id("tt1:2:3"), ("tt1", 2, 3))
        self.assertEqual(split_id("tt1375666"), ("tt1375666", None, None))
        for vid in ("kitsu:7442:5", "mal:52991:3", "kitsu:7442", "tmdb:1234"):
            self.assertEqual(split_id(vid)[0], None, vid)          # 'kitsu' was read as an IMDb id

    def test_an_id_in_other_digits_is_no_imdb_id_nor_episode(self):
        # "²" passed isdigit and raised ValueError out of int, as did a number longer than int reads;
        # other scripts' digits were read as ASCII ones (season 1, episode 2) or kept as an IMDb id
        for vid in ("tt1:\u00b2:3", "tt1:1:\u00b2", "tt1:\u0661:\u0662", "tt1:\uff11:2", "tt1:" + "1" * 5000 + ":2"):
            with self.subTest(vid=vid[:12]):
                self.assertEqual(split_id(vid), ("tt1", None, None))
        for vid in ("tt\u0661\u0662", "tt\uff11:1:2"):
            with self.subTest(vid=vid):
                self.assertEqual(split_id(vid), (None, None, None))

    def test_an_anime_episode_is_found_by_torrent_hash_and_release_name(self):
        hints = {"filename": "[SubsPlease] Show - 02 (1080p).mkv", "infoHash": HASH, "fileIdx": 1}
        got = self.extra().search("series", "kitsu:7442:2", hints=hints)
        self.assertFalse(any("kitsu" in p for p, _ in self.fake.calls))     # no source got the Kitsu id as an IMDb id
        self.assertFalse(self.fake.asked("/osv3") or self.fake.asked("/cinemeta") or self.fake.asked("/gestdown"))
        self.assertTrue(self.fake.asked(f"/tsdb/v1/by-infohash/{HASH}"))
        rel = next(q for p, q in self.fake.calls if p == "/tsdb/v1/by-releasename")
        self.assertEqual(rel["release"], "[SubsPlease] Show - 02 (1080p)")  # as it is, without the extension
        self.assertTrue({"tsdb-21", "tsdb-31"} <= {c["id"] for c in got})

    def test_two_threads_making_room_for_an_id_never_drop_the_same_one(self):
        # The ids table was changed by the pool's threads without a lock: two lookups at full capacity took
        # the same oldest entry, and the second one's pop failed
        ex = self.extra()
        first_look, both_looked = threading.Event(), threading.Event()

        class Held(dict):
            """Holds each eviction until another thread has looked for the oldest entry too (at most 0.5 s)."""
            looks = 0

            def __iter__(self):
                Held.looks += 1
                (both_looked if Held.looks > 1 else first_look).set()
                return super().__iter__()

            def pop(self, key, *default):
                both_looked.wait(0.5)
                return super().pop(key, *default)

        ex.ids = Held({("tvdb", "tt8"): (0, 8), ("tvdb", "tt9"): (0, 9)})
        errors = []

        def look(key):
            try:
                ex._id("tvdb", key, lambda: key)
            except Exception as e:
                errors.append(e)
        # daemon threads: one stuck on the ids' lock fails the test, never holds up the run's exit
        with mock.patch.object(srclimits, "SEARCH_CACHE_ENTRIES", 2):
            a = threading.Thread(target=look, args=("tt1",), daemon=True)
            a.start()
            first_look.wait(2)
            b = threading.Thread(target=look, args=("tt2",), daemon=True)
            b.start()
            a.join(5)
            b.join(5)
        self.assertEqual((a.is_alive(), b.is_alive()), (False, False))
        self.assertEqual(errors, [])
        self.assertEqual(set(ex.ids), {("tvdb", "tt1"), ("tvdb", "tt2")})

    def test_an_id_looked_up_again_is_kept_as_the_newest(self):
        # The same for the ids: a "none" looked up again after its hour went first
        ex = self.extra()
        with mock.patch.object(srclimits, "SEARCH_CACHE_ENTRIES", 2):
            ex._id("tvdb", "tt1", lambda: None)
            ex._id("tvdb", "tt2", lambda: 2)
            self.clock.t += srclimits.EMPTY_TTL_S + 1
            ex._id("tvdb", "tt1", lambda: None)
            ex._id("tvdb", "tt3", lambda: 3)
        self.assertEqual(set(ex.ids), {("tvdb", "tt1"), ("tvdb", "tt3")})

    def test_an_id_being_looked_up_is_waited_for(self):
        # Two threads needing the same id (a TVDB id, a SubSource id: a counted request) both looked it up
        ex = self.extra()
        started, release, calls, got = threading.Event(), threading.Event(), [], []

        def lookup(answer):
            calls.append(answer)
            started.set()
            release.wait(5)
            if isinstance(answer, Exception):
                raise answer
            return answer

        def need(key, answer):
            try:
                got.append(ex._id("subsource", key, lambda: lookup(answer)))
            except Exception as e:
                got.append(type(e).__name__)
        for key, answer in (("tt2", 555), ("tt3", ValueError())):
            with self.subTest(key=key):
                for state in (started, release, calls, got):
                    state.clear()
                a = threading.Thread(target=need, args=(key, answer), daemon=True)   # as above
                a.start()
                self.assertTrue(started.wait(5))
                b = threading.Thread(target=need, args=(key, answer), daemon=True)
                b.start()
                b.join(0.3)
                self.assertTrue(b.is_alive())                                    # waiting
                release.set()
                a.join(5)
                b.join(5)
                self.assertEqual((a.is_alive(), b.is_alive()), (False, False))
                self.assertEqual(len(calls), 1)
                self.assertEqual(got, [555, 555] if key == "tt2" else ["ValueError", "ValueError"])
                self.assertEqual(ex.looking, {})


class TheSubtitleDBTest(SourcesHarness, unittest.TestCase):
    def test_torrent_hash_matches_first_episode_filtered_duplicates_once(self):
        hints = {"filename": "Show.S01E02.1080p.WEB-NTb.mkv", "infoHash": HASH, "fileIdx": 1}
        got = [c for c in self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "tt2:1:2", hints=hints)]
        ids = [c["id"] for c in got]
        self.assertEqual(ids[:2], ["tsdb-21", "tsdb-23"])            # the torrent's own subtitles first
        self.assertNotIn("tsdb-22", ids)                              # another episode of the same torrent
        self.assertEqual(ids.count("tsdb-21"), 1)                     # also found by release name: once
        self.assertTrue(all(c["hash_match"] for c in got if c["id"] in ("tsdb-21", "tsdb-23")))
        rel = next(c for c in got if c["id"] == "tsdb-31")
        self.assertTrue(rel["release_match"] and not rel["hash_match"])
        self.assertIn("tsdb-41", ids)                                 # and by IMDb id
        self.assertEqual({c["lang"] for c in got}, {"eng", "rus"})

    def test_the_key_is_sent_in_a_header_never_in_an_address(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb", SUBSYNC_TSDB_KEY="sdb_k")
        got = ex.search("movie", "tt1")
        auth = [h.get("Authorization") for p, h in self.fake.headers if p.startswith("/tsdb/v1")]
        self.assertTrue(auth and all(a == "Bearer sdb_k" for a in auth))
        self.assertFalse(any("sdb_k" in p or "sdb_k" in json.dumps(q) for p, q in self.fake.calls))
        self.assertFalse(any("sdb_k" in c["url"] for c in got))
        cand = got[0]
        self.assertEqual(ex.headers_for(cand), {"Authorization": "Bearer sdb_k"})
        fetch_subtitle(cand["url"], headers=ex.headers_for(cand))
        self.assertEqual(self.fake.headers[-1][1].get("Authorization"), "Bearer sdb_k")
        self.assertEqual(self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").headers_for(cand), {})

    def test_one_lookup_failing_keeps_the_others_and_doesnt_count_as_a_failure(self):
        self.fake.mode["tsdb-release"] = "400"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        for _ in range(3):
            ex.cache.clear()
            got = ex.search("series", "tt2:1:2", hints={"filename": "Show.S01E02.mkv", "infoHash": HASH})
            self.assertIn("tsdb-41", [c["id"] for c in got])         # the IMDb lookup still counts
        self.assertEqual(ex.health.status(), {})

    def test_a_torrents_subtitles_are_hash_matches_only_for_a_known_episode(self):
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "kitsu:7442:2",
                                                               hints={"filename": "[G] Show - 02.mkv", "infoHash": HASH})
        self.assertTrue(got and not any(c["hash_match"] for c in got))    # a batch: which episode is unknown
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("movie", "tt1", hints={"infoHash": HASH})
        self.assertTrue(all(c["hash_match"] for c in got if c["id"] in ("tsdb-21", "tsdb-22")))

    def test_in_a_season_packs_torrent_only_the_played_episodes_subtitles_are_its_own(self):
        # A pack's subtitle named for no episode was taken as the played episode's own (a hash match)
        got = {c["id"]: c for c in self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "tt2:1:2",
                                                                                 hints={"infoHash": PACK})}
        self.assertTrue(got["tsdb-51"]["hash_match"])
        self.assertNotIn("tsdb-52", got)                                        # another episode's
        self.assertFalse(got["tsdb-53"]["hash_match"])                         # which episode is unknown
        self.assertIn("tsdb-53", got)                                           # but still a candidate
        got = {c["id"]: c for c in self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "tt2:1:2",
                                                                                 hints={"infoHash": SINGLE})}
        self.assertTrue(got["tsdb-61"]["hash_match"])                          # no batch: as before


class ReleaseNameTest(SourcesHarness, unittest.TestCase):
    """Seen in a capture: TheSubtitleDB answers a release name with the closest title it has, which can be
    another film (an Inception file name got Nomadland, score 0.57)."""

    def test_another_title_is_left_out(self):
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search(
            "movie", "tt1", hints={"filename": "Inception.2010.2160p.MA.WEB-DL.DTS-HD.MA.5.1.DV.HDR.H.265-TheFarm.mkv"})
        self.assertNotIn("tsdb-91", [c["id"] for c in got])
        self.assertEqual({c["id"] for c in got}, {"tsdb-11", "tsdb-12"})        # the IMDb lookup's

    def test_only_the_matched_subtitle_is_a_release_match(self):
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "tt2:1:2", hints={"filename": "Show.S01E02.x.mkv"})
        marked = [c["id"] for c in got if c["release_match"]]
        self.assertEqual(marked, ["tsdb-31"])
        self.assertIn("tsdb-32", [c["id"] for c in got])                       # the right title's others still count

    def test_a_weak_match_marks_nothing(self):
        self.fake.mode["score"] = "0.6"
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "tt2:1:2", hints={"filename": "Show.S01E02.x.mkv"})
        self.assertFalse([c for c in got if c["release_match"]])

    def test_without_an_imdb_id_only_a_strong_match_counts(self):
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("series", "kitsu:1:1", hints={"filename": "Show.S01E02.x.mkv"})
        self.assertEqual([c["id"] for c in got], ["tsdb-31"])                  # the title can't be checked: its others out
        self.fake.mode["score"] = "0.6"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        self.assertEqual(ex.search("series", "kitsu:1:1", hints={"filename": "Show.S01E02.x.mkv"}), [])


class StremioOpenSubtitlesTest(SourcesHarness, unittest.TestCase):
    def test_asked_like_a_player_with_the_files_hash(self):
        hints = {"filename": "Movie.2010.mkv", "videoSize": 123456, "videoHash": "abcd1234abcd1234"}
        got = self.extra(SUBSYNC_EXTRA_SOURCES="osv3").search("movie", "tt1", hints=hints)
        self.assertEqual([(c["id"], c["lang"], c["hash_match"]) for c in got],
                         [("osv3-101", "eng", True), ("osv3-102", "rus", False)])   # French left out
        path = self.fake.asked("/osv3/subtitles/")[0]
        self.assertEqual(path, "/osv3/subtitles/movie/tt1/videoHash=abcd1234abcd1234&videoSize=123456"
                               "&filename=Movie.2010.mkv.json")


class GestdownTest(SourcesHarness, unittest.TestCase):
    def test_a_language_without_subtitles_keeps_the_others(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        got = ex.search("series", "tt2:1:2")
        self.assertEqual([(c["id"], c["lang"]) for c in got], [("gd-G1", "eng")])   # Russian: 404, English kept
        self.assertEqual(ex.health.status(), {})

    def test_a_show_gestdown_doesnt_know_is_no_subtitles_not_a_failure(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        for _ in range(4):
            ex.cache.clear()
            self.assertEqual(ex.search("series", "tt3:1:2"), [])
        self.assertEqual(ex.health.status(), {})                    # four 404s: never paused

    def test_a_show_being_refreshed_is_asked_again_next_time(self):
        self.fake.mode["gestdown"] = "423"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        self.assertEqual(ex.search("series", "tt2:1:2"), [])
        n = len(self.fake.asked("/gestdown/subtitles/get/"))
        self.fake.mode.pop("gestdown")
        got = ex.search("series", "tt2:1:2")                         # not answered from the cache
        self.assertGreater(len(self.fake.asked("/gestdown/subtitles/get/")), n)
        self.assertEqual([c["id"] for c in got], ["gd-G1"])

    def test_too_many_requests_pauses_it(self):
        self.fake.mode["gestdown"] = "429"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        self.assertEqual(ex.search("series", "tt2:1:2"), [])
        self.assertIn("gestdown", ex.health.status())
        n = len(self.fake.calls)
        ex.cache.clear()
        ex.search("series", "tt2:1:2")
        self.assertEqual(len(self.fake.calls), n)                    # paused: not asked

    def test_a_show_gestdown_doesnt_know_is_asked_again_later(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        ex.search("series", "tt3:1:2")
        n = len(self.fake.asked("/gestdown/shows/"))
        ex.cache.clear()
        ex.search("series", "tt3:1:2")
        self.assertEqual(len(self.fake.asked("/gestdown/shows/")), n)       # remembered for a while
        self.clock.t += srclimits.EMPTY_TTL_S + 1
        ex.cache.clear()
        ex.search("series", "tt3:1:2")
        self.assertEqual(len(self.fake.asked("/gestdown/shows/")), n + 1)   # then asked again

    def test_cinemeta_asking_to_slow_down_doesnt_pause_gestdown(self):
        # Cinemeta's "too many requests", met while looking up the TVDB id, paused Gestdown for ten minutes
        for mode in ("429", "503-retry"):
            with self.subTest(mode=mode):
                self.fake.mode["cinemeta"] = mode
                ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
                self.assertEqual(ex.search("series", "tt2:1:2"), [])
                self.assertEqual(ex.health.status(), {})
                self.fake.mode.pop("cinemeta")
                self.assertEqual([c["id"] for c in ex.search("series", "tt2:1:2")], ["gd-G1"])   # nothing was kept

    def test_a_language_being_refreshed_beside_one_that_answered_is_asked_again_next_time(self):
        # Gestdown's answer was kept for hours when one language was being refreshed (423) and another
        # answered, so the first wasn't asked again as its docstring says
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        real = ex._get

        def get(url, *args, **kwargs):
            if url.endswith("/Russian"):
                raise Busy()
            return real(url, *args, **kwargs)
        with mock.patch.object(ex, "_get", side_effect=get):
            for n in (1, 2):
                self.assertEqual([c["id"] for c in ex.search("series", "tt2:1:2")], ["gd-G1"])
                self.assertEqual(len(self.fake.asked("/gestdown/subtitles/get/")), n)   # English, each time
        self.assertEqual(ex.health.status(), {})


class SubSourceTest(SourcesHarness, unittest.TestCase):
    def test_needs_a_key(self):
        self.assertEqual(self.extra(SUBSYNC_EXTRA_SOURCES="").search("series", "tt2:1:2"), [])
        self.assertFalse(self.fake.asked("/subsource"))

    def test_episode_filtered_machine_translated_marked_zip_opened_with_the_key(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="", SUBSYNC_SUBSOURCE_KEY="ss-key")
        got = ex.search("series", "tt2:1:2")
        self.assertEqual([c["id"] for c in got], ["subsource-7001", "subsource-7003", "subsource-7004"])  # not S01E03
        self.assertEqual([c["lang"] for c in got], ["eng", "rus", "eng"])
        self.assertTrue(got[2]["ai"] and not got[0]["ai"])
        q = next(q for p, q in self.fake.calls if p == "/subsource/movies/search")
        self.assertEqual((q["searchType"], q["imdb"], q["season"]), ("imdb", "tt2", "1"))
        q = next(q for p, q in self.fake.calls if p == "/subsource/subtitles")
        self.assertEqual((q["movieId"], q["language"]), ("555", "english,russian"))
        self.assertFalse(any("ss-key" in c["url"] for c in got))
        raw = fetch_subtitle(got[0]["url"], headers=ex.headers_for(got[0]))
        self.assertIn(b"Show.S01E02.srt", raw)                       # the zip's file for this episode

    def test_requests_are_counted_and_an_exhausted_limit_stops_it(self):
        ledger = Ledger(clock=self.clock, keyed={"subsource": True})
        self.addCleanup(ledger.close)
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="", SUBSYNC_SUBSOURCE_KEY="ss-key", ledger=ledger)
        ex.search("series", "tt2:1:2")
        self.assertEqual(ledger.used("subsource", "request", 86400), 2)
        ledger.count("subsource", "request", 58)                      # 60 a minute
        ex.cache.clear()
        n = len(self.fake.asked("/subsource"))
        self.assertEqual(ex.search("series", "tt2:1:2"), [])
        self.assertEqual(len(self.fake.asked("/subsource")), n)

    def test_a_request_without_room_in_the_limit_is_not_made(self):
        # A search checked the limit once, then counted each request as it made it: SubSource's two requests
        # at 59 of its 60 a minute made 61. Each request takes its room first now; the second finds none, so it isn't
        # made, nothing fails and nothing is kept: asked again next time
        ledger = Ledger(clock=self.clock, keyed={"subsource": True})
        self.addCleanup(ledger.close)
        ledger.count("subsource", "request", 59)
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="", SUBSYNC_SUBSOURCE_KEY="ss-key", ledger=ledger)
        self.assertEqual(ex.search("series", "tt2:1:2"), [])
        self.assertEqual(ledger.used("subsource", "request", 60), 60)
        self.assertEqual([p for p, _ in self.fake.calls if p.startswith("/subsource")], ["/subsource/movies/search"])
        self.assertEqual(ex.health.status(), {})                              # no failure, no pause
        self.clock.t += 60                                                     # the next minute has room
        self.assertEqual([c["id"] for c in ex.search("series", "tt2:1:2")],
                         ["subsource-7001", "subsource-7003", "subsource-7004"])
        self.assertEqual(ledger.used("subsource", "request", 60), 1)          # the id was kept: one request


class WaitPauseCacheTest(SourcesHarness, unittest.TestCase):
    def test_a_slow_source_is_left_out_and_its_late_answer_kept_for_next_time(self):
        self.fake.mode["tsdb"] = "slow"
        self.fake.slow_s = 1.2
        ex = self.extra(clock=time.time, SUBSYNC_EXTRA_SOURCES="tsdb,osv3", SUBSYNC_SOURCE_TIMEOUT_S="0.4")
        t0 = time.monotonic()
        got = ex.search("movie", "tt1")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual({c["source"] for c in got}, {"osv3"})

        def kept():
            with ex.cache_lock:
                return any(key[0] == "tsdb" for key in ex.cache)
        self.assertTrue(wait_until(kept, timeout=30))                # the slow answer arrives in the background
        n = len(self.fake.asked("/tsdb"))
        got = ex.search("movie", "tt1")
        self.assertEqual({c["source"] for c in got}, {"osv3", "tsdb"})
        self.assertEqual(len(self.fake.asked("/tsdb")), n)           # from the cache

    def test_an_answer_that_trickles_past_a_minute_counts_as_failing(self):
        # Each read waited at most the source's wait, the whole answer for ever, so a site that sent its answer
        # a byte now and then held one of the search threads as long as it liked and never counted as failing; now its
        # whole request ends at srclimits.ANSWER_WAIT_MAX_S (a minute), and three such answers pause it
        self.assertEqual(srclimits.ANSWER_WAIT_MAX_S, 60)
        self.fake.mode["tsdb"] = "trickle"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        with mock.patch.object(srclimits, "ANSWER_WAIT_MAX_S", 1.0):
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                ex._get(f"{self.fake.url}/tsdb/v1/by-imdb/tt1")
            self.assertLess(time.monotonic() - t0, 3)
            for _ in range(srclimits.PAUSE_FAILURES):
                self.assertEqual(ex.search("movie", "tt1"), [])
        self.assertIn("tsdb", ex.health.status())
        self.fake.mode.pop("tsdb")                                    # an answer within the wait is read whole
        self.assertEqual(len(ex._get(f"{self.fake.url}/tsdb/v1/by-imdb/tt1")["subtitles"]["items"]), 2)

    def test_a_failing_source_is_paused_then_tried_once(self):
        self.fake.mode["tsdb"] = "500"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        for _ in range(3):
            ex.search("movie", "tt1")
        self.assertIn("tsdb", ex.health.status())
        n = len(self.fake.asked("/tsdb"))
        ex.search("movie", "tt1")
        self.assertEqual(len(self.fake.asked("/tsdb")), n)           # paused
        self.clock.t += srclimits.PAUSE_S + 1
        ex.search("movie", "tt1")                                     # one trial, which fails: paused again
        self.assertEqual(len(self.fake.asked("/tsdb")), n + 1)
        self.assertIn("tsdb", ex.health.status())
        self.fake.mode.pop("tsdb")
        self.clock.t += srclimits.PAUSE_S + 1
        self.assertTrue(ex.search("movie", "tt1"))                    # the trial works: open again
        self.assertEqual(ex.health.status(), {})

    def test_answers_are_kept_for_hours_and_nothing_found_for_an_hour(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        ex.search("movie", "tt1")
        ex.search("movie", "tt9")                                     # nothing found
        n = len(self.fake.calls)
        ex.search("movie", "tt1")
        ex.search("movie", "tt9")
        self.assertEqual(len(self.fake.calls), n)
        self.clock.t += srclimits.EMPTY_TTL_S + 1
        ex.search("movie", "tt1")
        self.assertEqual(len(self.fake.calls), n)                     # still kept
        ex.search("movie", "tt9")
        self.assertEqual(len(self.fake.calls), n + 1)                 # asked again
        self.clock.t += srclimits.SEARCH_TTL_S - srclimits.EMPTY_TTL_S
        ex.search("movie", "tt1")
        self.assertEqual(len(self.fake.calls), n + 2)                 # hours later, a found answer is asked again too

    def test_answers_are_kept_for_the_latest_titles_only(self):
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        with mock.patch.object(srclimits, "SEARCH_CACHE_ENTRIES", 2):
            for title in ("tt1", "tt8", "tt9"):
                ex.search("movie", title)
            self.assertEqual(len(ex.cache), 2)
            n = len(self.fake.calls)
            ex.search("movie", "tt9")
            self.assertEqual(len(self.fake.calls), n)                 # kept
            ex.search("movie", "tt1")
            self.assertEqual(len(self.fake.calls), n + 1)             # the oldest went to make room: asked again

    def test_thesubtitledb_is_asked_for_a_few_pages_at_most(self):
        got = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search("movie", "tt7")
        self.assertEqual(len(self.fake.asked("/tsdb/v1/by-imdb/tt7")), srclimits.TSDB_PAGES)   # of 50
        self.assertEqual(len(got), 2 * srclimits.TSDB_PAGES)

    def test_a_source_the_config_already_has_is_skipped(self):
        self.extra(SUBSYNC_EXTRA_SOURCES="tsdb,osv3").search("movie", "tt1", skip={"osv3"})
        self.assertFalse(self.fake.asked("/osv3"))
        self.assertTrue(self.fake.asked("/tsdb"))

    def test_a_503_without_retry_after_counts_as_a_failure(self):
        # One 503 (a restart) paused the source for everyone at once; only "too many requests" (429, or 503
        # with Retry-After) does
        self.fake.mode["tsdb"] = "503"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        ex.search("movie", "tt1")
        self.assertEqual(ex.health.status(), {})
        for _ in range(srclimits.PAUSE_FAILURES - 1):
            ex.search("movie", "tt1")
        self.assertIn("tsdb", ex.health.status())                   # then like any failure: three pause it
        self.fake.mode["tsdb"] = "503-retry"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        ex.search("movie", "tt1")
        self.assertIn("tsdb", ex.health.status())                   # asked to come back later: paused at once

    def test_a_call_that_asks_a_paused_source_nothing_keeps_its_pause(self):
        # After the pause, a request whose id Gestdown can't use (Kitsu) took the one trial without asking it,
        # and its "answer" opened Gestdown again
        self.fake.mode["gestdown"] = "500"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        for _ in range(srclimits.PAUSE_FAILURES):
            ex.search("series", "tt2:1:2")
        self.assertIn("gestdown", ex.health.status())
        self.clock.t += srclimits.PAUSE_S + 1
        n = len(self.fake.asked("/gestdown"))
        with self.assertNoLogs("subsync.sources", "INFO"):
            self.assertEqual(ex.search("series", "kitsu:7442:2"), [])
        self.assertEqual(len(self.fake.asked("/gestdown")), n)                 # not asked
        ex.search("series", "tt2:1:2")                                          # the trial, which fails: paused again
        self.assertEqual(len(self.fake.asked("/gestdown")), n + 1)
        self.assertIn("gestdown", ex.health.status())

    def test_an_answer_asked_again_is_kept_as_the_newest(self):
        # An answer stored again after its time kept its first place, so it was the next one dropped
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        with mock.patch.object(srclimits, "SEARCH_CACHE_ENTRIES", 2):
            ex.search("movie", "tt1")
            ex.search("movie", "tt8")
            self.clock.t += srclimits.SEARCH_TTL_S + 1
            ex.search("movie", "tt1")                                 # asked again: the newest now
            ex.search("movie", "tt9")                                 # makes room: tt8, the oldest, goes
            n = len(self.fake.calls)
            self.assertEqual(len(ex.search("movie", "tt1")), 2)
            self.assertEqual(len(self.fake.calls), n)                 # kept

    def test_another_release_of_the_episode_reuses_the_answers_not_asked_by_file(self):
        # Every answer was kept under the file's hints, so each release of an episode asked SubDL, SubSource
        # and Gestdown again for the same answer
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="osv3,gestdown", SUBSYNC_SUBDL_KEY="k1", SUBSYNC_SUBSOURCE_KEY="ss-key")
        first = {"filename": "Show.S01E02.1080p.WEB-GRP.mkv", "videoHash": "ab" * 8, "videoSize": 5}
        for name in (first["filename"], "Show.S01E02.720p.HDTV-LOL.mkv", "Show.S01E02.2160p.WEB-NTb.mkv"):
            ex.search("series", "tt2:1:2", hints=dict(first, filename=name))
        for path in ("/subdl/api/v1/subtitles", "/subsource/subtitles", "/gestdown/subtitles/get/S1/1/2/English"):
            self.assertEqual(len(self.fake.asked(path)), 1, path)
        self.assertEqual(len(self.fake.asked("/osv3/subtitles/")), 3)            # asked by the file: once per file
        got = ex.search("series", "tt2:1:2", langs=["fre"], hints=first)
        self.assertEqual([c["id"] for c in got if c["source"] == "osv3"], ["osv3-103"])
        self.assertEqual(len(self.fake.asked("/osv3/subtitles/")), 3)            # it lists every language: kept

    def test_a_search_while_the_same_ask_runs_waits_for_it(self):
        # A search whose answer was still on its way (a source slower than SUBSYNC_SOURCE_TIMEOUT_S) asked the
        # source again
        self.fake.mode["tsdb"] = "slow"
        ex = self.extra(clock=time.time, SUBSYNC_EXTRA_SOURCES="tsdb", SUBSYNC_SOURCE_TIMEOUT_S="0.2")
        self.assertEqual(ex.search("movie", "tt1"), [])                          # too slow: left out
        self.assertEqual(ex.search("movie", "tt1"), [])                          # still running: waited for

        def kept():
            with ex.cache_lock:
                return any(key[0] == "tsdb" for key in ex.cache)
        self.assertTrue(wait_until(kept, timeout=10))
        self.assertEqual(len(self.fake.asked("/tsdb/v1/by-imdb/tt1")), 1)
        self.assertEqual(len(ex.search("movie", "tt1")), 2)                      # its answer, kept
        self.assertEqual(len(self.fake.asked("/tsdb/v1/by-imdb/tt1")), 1)

    def test_an_answer_part_of_which_failed_is_kept_an_hour(self):
        # A language (Gestdown) or a lookup (TheSubtitleDB) that failed beside others that answered: their
        # answer was kept for hours as if whole; now as "nothing found" is
        self.fake.mode["tsdb-release"] = "400"
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb,gestdown")
        real = ex._get

        def get(url, *args, **kwargs):
            if url.endswith("/Russian"):
                raise closed(urllib.error.HTTPError(url, 500, "down", None, None))
            return real(url, *args, **kwargs)
        hints = {"filename": "Show.S01E02.mkv", "infoHash": HASH}
        with mock.patch.object(ex, "_get", side_effect=get):
            ex.search("series", "tt2:1:2", hints=hints)
            n = len(self.fake.calls)
            got = ex.search("series", "tt2:1:2", hints=hints)
            self.assertEqual(len(self.fake.calls), n)                            # kept for a while
            self.assertTrue({"tsdb-41", "gd-G1"} <= {c["id"] for c in got})
            self.clock.t += srclimits.EMPTY_TTL_S + 1
            ex.search("series", "tt2:1:2", hints=hints)
        self.assertEqual(len(self.fake.asked("/tsdb/v1/by-imdb/")), 2)            # then asked again
        self.assertEqual(len(self.fake.asked("/gestdown/subtitles/get/")), 2)
        self.assertEqual(ex.health.status(), {})


class DownloadTest(SourcesHarness, unittest.TestCase):
    """A candidate's download: its address made only from a path on the source's own host, and a source's key
    sent only there, never on through a redirect, never for a person's own subtitle."""

    def test_a_key_goes_only_to_its_sources_own_origin(self):
        # The key was chosen by the candidate's source label alone, so an http address on SubSource's domain
        # or a TheSubtitleDB download_url on another host got it
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb", SUBSYNC_SUBSOURCE_KEY="ss-key", SUBSYNC_TSDB_KEY="sdb_k",
                        SUBSYNC_SUBSOURCE_URL="https://api.subsource.net/api/v1")
        own = "https://api.subsource.net/api/v1/subtitles/7/download"
        self.assertEqual(ex.headers_for({"source": "subsource", "url": own}), {"X-API-Key": "ss-key"})
        for url in ("http://api.subsource.net/api/v1/subtitles/7/download", "https://other.subsource.net/x",
                    "https://api.subsource.net:8443/api/v1/subtitles/7/download"):
            with self.subTest(url=url):
                self.assertEqual(ex.headers_for({"source": "subsource", "url": url}), {})
        self.assertEqual(ex.headers_for({"source": "tsdb", "url": f"{self.fake.url}/tsdb/get/1"}),
                         {"Authorization": "Bearer sdb_k"})
        self.assertEqual(ex.headers_for({"source": "tsdb", "url": "https://cdn.other.example/get/1"}), {})

    def test_a_redirect_to_another_origin_leaves_the_key_behind(self):
        # Urllib sends every header on to wherever a redirect points
        other = FakeSources()
        self.addCleanup(other.close)
        keys = {"X-API-Key": "ss-key", "Authorization": "Bearer sdb_k"}
        for fake, kept in ((other, False), (self.fake, True)):
            target = f"{fake.url}/tsdb/get/1"
            with self.subTest(kept=kept):
                fetch_subtitle(f"{self.fake.url}/hop?to={urllib.parse.quote(target, safe='')}", headers=keys)
                path, got = fake.headers[-1]
                got = {k.lower(): v for k, v in got.items()}
                self.assertEqual(path, "/tsdb/get/1")
                self.assertEqual((got.get("x-api-key"), got.get("authorization")),
                                 ("ss-key", "Bearer sdb_k") if kept else (None, None))

    def test_a_persons_own_subtitle_on_a_keyed_site_goes_without_the_key(self):
        # A person's own list could name any address on SubSource's or TheSubtitleDB's API, and it was asked
        # with the instance's key, its answer served back as an "(original)"
        ex = self.extra(SUBSYNC_SUBSOURCE_KEY="ss-key", SUBSYNC_TSDB_KEY="sdb_k",
                        SUBSYNC_SUBSOURCE_URL="https://api.subsource.net/api/v1",
                        SUBSYNC_TSDB_URL="https://api.thesubtitledb.org")
        own = {"subsource": "https://api.subsource.net/api/v1/subtitles/7/download",
               "tsdb": "https://api.thesubtitledb.org/get/1"}
        listed = {"subsource": "https://api.subsource.net/api/v1/anything/the/person/chose?x=1",
                  "tsdb": "https://api.thesubtitledb.org/v1/account"}
        for source, key in (("subsource", {"X-API-Key": "ss-key"}), ("tsdb", {"Authorization": "Bearer sdb_k"})):
            with self.subTest(source=source):
                self.assertEqual(ex.headers_for({"source": source, "url": own[source]}), key)   # found here
                for url in (own[source], listed[source]):
                    self.assertEqual(ex.headers_for({"source": source, "url": url, "from_config": True}), {})

    def test_a_download_path_that_is_no_path_is_left_out(self):
        # A site's field was put after its address as it came: "@host/…" moved the download to another host,
        # a missing one made "…None"
        ex = self.extra(SUBSYNC_SUBDL_KEY="k1")
        answer = {"subtitles": [{"url": url, "language": "EN", "release_name": "x"}
                                for url in ("@evil.example/subtitle/1.zip", ".evil.example/subtitle/2.zip", None, 7, "",
                                            "/subtitle/3-4.zip")]}
        with mock.patch.object(ex, "_get", return_value=answer):
            got = ex._subdl("series", "tt2:1:2", ["eng"], {})
        self.assertEqual([c["url"] for c in got], [f"{self.fake.url}/subdl-dl/subtitle/3-4.zip#S01E02"])
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="gestdown")
        real = ex._get

        def get(url, *args, **kwargs):
            if "/subtitles/get/" not in url:
                return real(url, *args, **kwargs)
            items = ({}, {"downloadUri": None}, {"downloadUri": "@evil.example/x"}, {"downloadUri": 5},
                     {"downloadUri": "/subtitles/download/G9"})
            return {"matchingSubtitles": [dict(it, subtitleId=f"G{i}", completed=True) for i, it in enumerate(items)]}
        with mock.patch.object(ex, "_get", side_effect=get):
            got = ex.search("series", "tt2:1:2")
        self.assertEqual([(c["id"], c["lang"], c["url"]) for c in got],
                         [("gd-G4", lang, f"{self.fake.url}/gestdown/subtitles/download/G9") for lang in ("eng", "rus")])


class StateTest(unittest.TestCase):
    def test_ledger_windows_and_fair_share(self):
        clock = Clock()
        ledger = Ledger(clock=clock, keyed={"subdl": True})
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.limits("subdl", "download"), [(86400, 300)])
        for _ in range(200):
            ledger.count("subdl", "download", who="a")
        self.assertFalse(ledger.tight("subdl", "download", who="b"))   # under 80 %
        for _ in range(45):
            ledger.count("subdl", "download", who="b")
        self.assertTrue(ledger.tight("subdl", "download", who="a"))     # 245 >= 240: a had more than half
        self.assertFalse(ledger.tight("subdl", "download", who="c"))
        self.assertFalse(ledger.exhausted("subdl", "download"))
        ledger.count("subdl", "download", 55)
        self.assertTrue(ledger.exhausted("subdl", "download"))
        clock.t += 86400                                                # a new UTC day
        self.assertFalse(ledger.exhausted("subdl", "download"))
        self.assertEqual(ledger.limits("osv3", "download"), [])          # no published limit: nothing counted

    def test_a_window_is_read_from_one_reading_of_the_clock(self):
        # Used read the clock twice; a second that began between the two readings looked in a window that
        # doesn't exist, so a full window read as empty and let a request past the site's limit
        clock = Clock()
        clock.t += 39.9999                                              # the end of second 39 of a minute window
        ledger = Ledger(clock=clock, keyed={"subsource": True})
        self.addCleanup(ledger.close)
        ledger.count("subsource", "request", 60)                        # SubSource's minute is full
        ledger.clock = Ticking(clock.t, clock.t + 0.0001)
        self.assertEqual(ledger.used("subsource", "request", 60), 60)
        ledger.clock = Ticking(clock.t, clock.t, clock.t + 0.0001)
        self.assertTrue(ledger.exhausted("subsource", "request"))
        ledger.clock = Ticking(clock.t, clock.t, clock.t + 0.0001)
        self.assertTrue(ledger.tight("subsource", "request"))

    def test_room_is_taken_at_once_so_requests_made_together_cant_pass_a_limit(self):
        # The limit was checked (exhausted) and the request counted later (count), so requests made at the
        # same time all passed the same room. take() checks every window and counts in one step
        clock = Clock()
        ledger = Ledger(clock=clock, keyed={"subsource": True})
        self.addCleanup(ledger.close)
        ledger.count("subsource", "request", 50)
        with ThreadPoolExecutor(16) as pool:
            taken = list(pool.map(lambda _: ledger.take("subsource", "request", who="me"), range(40)))
        self.assertEqual(taken.count(True), 10)
        self.assertEqual((ledger.used("subsource", "request", 60), ledger.used("subsource", "request", 60, "me")),
                         (60, 10))
        self.assertFalse(ledger.take("subsource", "request"))
        clock.t += 60                                                   # the next minute
        ledger.count("subsource", "request", 59)
        self.assertFalse(ledger.take("subsource", "request", 2))       # room for one, not two: nothing counted
        self.assertEqual(ledger.used("subsource", "request", 60), 59)
        self.assertTrue(ledger.take("subsource", "request"))
        self.assertEqual(ledger.used("subsource", "request", 3600), 120)   # the hour's window counted each one too
        self.assertTrue(ledger.take("osv3", "download"))                # no published limit: always room, nothing kept
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM counts WHERE source='osv3'").fetchone()[0], 0)

    def test_a_count_is_one_transaction_of_a_wal_database(self):
        # Each window's row, and the person's, was a transaction of its own in a rollback journal synced in
        # full: six disk flushes for one SubSource request, under the lock every limit check waits for
        with tempfile.TemporaryDirectory() as data:
            ledger = Ledger(os.path.join(data, "sources.sqlite3"), clock=Clock(), keyed={"subsource": True})
            try:
                self.assertEqual(ledger.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(ledger.db.execute("PRAGMA synchronous").fetchone()[0], 1)     # NORMAL
                statements = []
                ledger.db.set_trace_callback(statements.append)
                self.assertTrue(ledger.take("subsource", "request", who="me"))
                ledger.count("subsource", "request", who="me")
                ledger.db.set_trace_callback(None)
                self.assertEqual([x for x in statements if not x.startswith(("INSERT", "SELECT"))],
                                 ["BEGIN IMMEDIATE", "DELETE FROM counts WHERE start < 1799827200", "COMMIT",
                                  "BEGIN IMMEDIATE", "COMMIT"])           # the sweep once, with the first
                self.assertEqual(ledger.db.execute("SELECT COUNT(*), SUM(n) FROM counts").fetchone(), (6, 12))
            finally:
                ledger.close()

    def test_the_sweep_drops_windows_that_started_over_two_days_ago(self):
        clock = Clock(1_799_971_200.0)                                  # 00:00 UTC
        ledger = Ledger(clock=clock, keyed={"subdl": True})
        self.addCleanup(ledger.close)
        ledger.count("subdl", "download", 7)                            # day 0
        clock.t += sourcestate.LEDGER_KEEP_S - 1                        # nearly two days on: kept
        ledger.count("subdl", "download")
        self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM counts").fetchone()[0], 2)
        clock.t += sourcestate.LEDGER_SWEEP_S + 2                       # an hour later: day 0's window goes
        ledger.count("subdl", "download")
        self.assertEqual(ledger.db.execute("SELECT SUM(n) FROM counts").fetchone()[0], 2)

    def test_a_retry_after_that_is_no_number_pauses_for_the_default(self):
        # "nan" paused nothing (every comparison with it is false); a number is kept within the bounds
        for asked, pause in (("nan", srclimits.PAUSE_S), ("soon", srclimits.PAUSE_S), (None, srclimits.PAUSE_S),
                             ("5", srclimits.RETRY_AFTER_MIN_S), ("120", 120), ("inf", srclimits.RETRY_AFTER_MAX_S)):
            with self.subTest(asked=asked):
                clock = Clock()
                h = Health(clock)
                h.limited("x", asked)
                self.assertEqual(h.status(), {"x": clock.t + pause})

    def test_health_trial_after_a_pause(self):
        clock = Clock()
        h = Health(clock)
        for _ in range(3):
            self.assertTrue(h.allowed("x"))
            h.failed("x")
        self.assertFalse(h.allowed("x"))
        clock.t += srclimits.PAUSE_S + 1
        self.assertTrue(h.allowed("x"))
        self.assertFalse(h.allowed("x"))                                # one trial at a time
        h.ok("x")
        self.assertTrue(h.allowed("x"))
        h.limited("y", "30")
        self.assertFalse(h.allowed("y"))
        clock.t += 61
        self.assertTrue(h.allowed("y"))

    def test_config_sources(self):
        config = {"presets": [{"type": "opensubtitles", "enabled": True, "options": {"resources": ["subtitles"]}},
                              {"type": "subdl", "enabled": False, "options": {}},
                              {"type": "custom", "enabled": True, "options": {"manifestUrl": "https://api.gestdown.info/x/manifest.json"}},
                              {"type": "torrentio", "enabled": True, "options": {}}]}
        self.assertEqual(srclimits.config_sources(config), {"osv3", "gestdown"})
        self.assertEqual(srclimits.source_of_url("https://subs5.strem.io/en/download/x"), "osv3")
        self.assertEqual(srclimits.source_of_url("https://opensubtitles-v3.strem.io/manifest.json"), "osv3")
        self.assertEqual(srclimits.source_of_url("https://strem.io/x"), "osv3")
        self.assertEqual(srclimits.source_of_url("https://evil-strem.io.example.com/x"), None)


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = Settings({})
        self.assertEqual(s.extra_sources, ["osv3", "osv3plus", "tsdb", "gestdown"])
        self.assertEqual(s.osv3_url, "https://opensubtitles-v3.strem.io")
        self.assertEqual(s.subsource_url, "https://api.subsource.net/api/v1")
        self.assertEqual((s.tsdb_key, s.subsource_key), ("", ""))
        self.assertEqual(s.source_timeout, 5.0)
        self.assertTrue(s.skip_config_sources)

    def test_no_wyzie_in_public_mode(self):
        self.assertEqual(Settings({"SUBSYNC_WYZIE_KEY": "k"}).conflicts(), [])
        got = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_WYZIE_KEY": "k"}).conflicts()
        self.assertTrue(any("Wyzie" in c for c in got))


if __name__ == "__main__":
    unittest.main()
