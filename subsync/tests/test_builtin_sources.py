"""The subtitle sources AIOSubtitles asks itself. A local stand-in answers like TheSubtitleDB (by IMDb id,
torrent hash and release name), Stremio's OpenSubtitles v3, Cinemeta + Gestdown and SubSource; no network (a test that
opens a connection to anything but this machine fails)."""
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import local_network_only  # noqa: E402
from subsync import srclimits  # noqa: E402
from subsync.extra import ExtraSources, split_id  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.sourcestate import Health, Ledger  # noqa: E402
from subsync.sources import fetch_subtitle  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n"
HASH = "a" * 40


def zipped(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, SRT.replace("Hello", n))
    return buf.getvalue()


def tsdb_item(i, lang, release, cues=900):
    return {"id": i, "language": lang, "format": "srt", "release_name": release, "cues": cues, "hearing_impaired": False,
            "ai": None, "ai_transcribed": False, "download_url": f"{{url}}/tsdb/get/{i}"}


class Fake:
    """Answers like the sources. `mode` per source: None, "500", "404", "423", "429", "slow"."""

    def __init__(self):
        self.calls, self.headers, self.mode, self.slow_s = [], [], {}, 1.5
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json", extra=None):
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def items(self, rows):
                return {"subtitles": {"total": len(rows), "limit": 100, "offset": 0,
                                      "items": [dict(r, download_url=r["download_url"].format(url=outer.url)) for r in rows]}}

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                outer.calls.append((u.path, q))
                outer.headers.append((u.path, dict(self.headers)))
                top = u.path.split("/")[1]
                mode = outer.mode.get(top)
                if mode == "slow":
                    time.sleep(outer.slow_s)
                elif mode == "500":
                    return self.send(500, {"error": "down"})
                elif mode == "429":
                    return self.send(429, {"error": "slow down"}, extra={"Retry-After": "120"})
                p = u.path
                # TheSubtitleDB
                if p == "/tsdb/v1/by-imdb/tt1":
                    return self.send(200, self.items([tsdb_item(11, "en", "Movie.2010.WEB-DL"),
                                                      tsdb_item(12, "ru", "Movie.2010.BluRay", 870)]))
                if p == "/tsdb/v1/by-imdb/tt2/season/1/episode/2":
                    return self.send(200, self.items([tsdb_item(41, "en", "Show.S01E02.HDTV")]))
                if p == f"/tsdb/v1/by-infohash/{HASH}":
                    return self.send(200, self.items([tsdb_item(21, "en", "Show.S01E02.1080p.WEB-NTb"),
                                                      tsdb_item(22, "en", "Show.S01E03.1080p.WEB-NTb"),
                                                      tsdb_item(23, "ru", "Show.S01E02.1080p.WEB-NTb")]))
                if p == "/tsdb/v1/by-releasename":
                    if outer.mode.get("tsdb-release") == "400":
                        return self.send(400, {"error": "bad release"})
                    # the real answer (capture): the title TheSubtitleDB matched, its closest subtitle and score,
                    # and that title's subtitles; for a name it doesn't know it matches another title
                    rel = q.get("release", "")
                    if "Inception" in rel:
                        return self.send(200, dict(self.items([tsdb_item(91, "en", "Nomadland.2020.1080p.WEB")]),
                                                   match={"sub_id": 91, "score": 0.57}, title={"imdb": "tt9770150"}))
                    return self.send(200, dict(self.items([tsdb_item(31, "en", rel), tsdb_item(32, "en", "Show.S01E02.HDTV"),
                                                           tsdb_item(21, "en", "Show.S01E02.1080p.WEB-NTb")]),
                                               match={"sub_id": 31, "score": float(outer.mode.get("score", 0.95))},
                                               title={"imdb": "tt2"}))
                if p.startswith("/tsdb/get/"):
                    return self.send(200, SRT.encode(), "text/plain")
                # Stremio's OpenSubtitles v3
                if p.startswith("/osv3/subtitles/"):
                    return self.send(200, {"subtitles": [
                        {"id": "101", "url": f"{outer.url}/osv3/file/101", "lang": "eng", "m": "h", "g": "1"},
                        {"id": "102", "url": f"{outer.url}/osv3/file/102", "lang": "rus", "m": "i", "g": "1"},
                        {"id": "103", "url": f"{outer.url}/osv3/file/103", "lang": "fre", "m": "i", "g": "1"}]})
                if p.startswith("/osv3/file/"):
                    return self.send(200, SRT.encode(), "text/plain")
                # Cinemeta + Gestdown
                if p == "/cinemeta/meta/series/tt2.json":
                    return self.send(200, {"meta": {"name": "Show", "tvdb_id": 121361}})
                if p == "/cinemeta/meta/series/tt3.json":
                    return self.send(200, {"meta": {"name": "Unknown", "tvdb_id": 999}})
                if p == "/gestdown/shows/external/tvdb/121361":
                    return self.send(200, {"shows": [{"id": "S1", "name": "Show"}]})
                if p == "/gestdown/shows/external/tvdb/999":
                    return self.send(404, {"error": "show not found"})
                if p.startswith("/gestdown/subtitles/get/S1/1/2/"):
                    if mode == "423":
                        return self.send(423, {"error": "refreshing"})
                    lang = p.rsplit("/", 1)[1]
                    if lang != "English":
                        return self.send(404, {"error": "no subtitles"})
                    return self.send(200, {"matchingSubtitles": [
                        {"subtitleId": "G1", "version": "WEB.NTb", "completed": True, "hearingImpaired": False,
                         "downloadUri": "/subtitles/download/G1"}]})
                # SubSource
                if p.startswith("/subsource/"):
                    if self.headers.get("X-API-Key") != "ss-key":
                        return self.send(401, {"error": "key"})
                    if p == "/subsource/movies/search":
                        return self.send(200, {"data": [{"id": 555, "title": "Show"}]})
                    if p == "/subsource/subtitles":
                        return self.send(200, {"data": [
                            {"subtitleId": 7001, "language": "english", "releaseInfo": ["Show.S01E02.1080p.WEB-NTb"],
                             "hearingImpaired": False},
                            {"subtitleId": 7002, "language": "english", "releaseInfo": ["Show.S01E03.1080p.WEB"]},
                            {"subtitleId": 7003, "language": "russian", "releaseInfo": "Show.S01.Complete"},
                            {"subtitleId": 7004, "language": "english", "releaseInfo": ["Show.S01E02.WEB"],
                             "productionType": "machine translated"}]})
                    if p == "/subsource/subtitles/7001/download":
                        return self.send(200, zipped(["Show.S01E01.srt", "Show.S01E02.srt"]), "application/zip")
                return self.send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def asked(self, prefix):
        return [p for p, _ in self.calls if p.startswith(prefix)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class SourcesBase(unittest.TestCase):
    def setUp(self):
        self.guard = local_network_only()
        self.guard.start()
        self.fake = Fake()
        u = self.fake.url
        self.env = {"SUBSYNC_LANGUAGES": "eng,rus", "SUBSYNC_TSDB_URL": f"{u}/tsdb", "SUBSYNC_CINEMETA_URL": f"{u}/cinemeta",
                    "SUBSYNC_GESTDOWN_URL": f"{u}/gestdown", "SUBSYNC_OSV3_URL": f"{u}/osv3",
                    "SUBSYNC_OSV3PLUS_URL": f"{u}/osv3plus", "SUBSYNC_SUBDL_URL": f"{u}/subdl",
                    "SUBSYNC_SUBDL_DOWNLOAD_URL": f"{u}/subdl-dl", "SUBSYNC_WYZIE_URL": f"{u}/wyzie",
                    "SUBSYNC_SUBSOURCE_URL": f"{u}/subsource"}
        self.clock = Clock()

    def tearDown(self):
        self.fake.close()
        self.guard.stop()

    def extra(self, clock=None, ledger=None, **env):
        return ExtraSources(Settings(dict(self.env, **env)), clock=clock or self.clock, ledger=ledger)


class IdsTest(SourcesBase):
    def test_only_imdb_ids_are_sent_as_imdb_ids(self):
        self.assertEqual(split_id("tt1:2:3"), ("tt1", 2, 3))
        self.assertEqual(split_id("tt1375666"), ("tt1375666", None, None))
        for vid in ("kitsu:7442:5", "mal:52991:3", "kitsu:7442", "tmdb:1234"):
            self.assertEqual(split_id(vid)[0], None, vid)          # 'kitsu' was read as an IMDb id

    def test_an_anime_episode_is_found_by_torrent_hash_and_release_name(self):
        hints = {"filename": "[SubsPlease] Show - 02 (1080p).mkv", "infoHash": HASH, "fileIdx": 1}
        got = self.extra().search("series", "kitsu:7442:2", hints=hints)
        self.assertFalse(any("kitsu" in p for p, _ in self.fake.calls))     # no source got the Kitsu id as an IMDb id
        self.assertFalse(self.fake.asked("/osv3") or self.fake.asked("/cinemeta") or self.fake.asked("/gestdown"))
        self.assertTrue(self.fake.asked(f"/tsdb/v1/by-infohash/{HASH}"))
        rel = next(q for p, q in self.fake.calls if p == "/tsdb/v1/by-releasename")
        self.assertEqual(rel["release"], "[SubsPlease] Show - 02 (1080p)")  # as it is, without the extension
        self.assertTrue({"tsdb-21", "tsdb-31"} <= {c["id"] for c in got})


class TheSubtitleDBTest(SourcesBase):
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


class ReviewFixesTest(SourcesBase):
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


class ReleaseNameTest(SourcesBase):
    """'s capture: TheSubtitleDB answers a release name with the closest title it has, which can be another film
    (an Inception file name got Nomadland, score 0.57)."""

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
        self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="tsdb")
        self.assertEqual(ex.search("series", "kitsu:1:1", hints={"filename": "Show.S01E02.x.mkv"}), [])


class StremioOpenSubtitlesTest(SourcesBase):
    def test_asked_like_a_player_with_the_files_hash(self):
        hints = {"filename": "Movie.2010.mkv", "videoSize": 123456, "videoHash": "abcd1234abcd1234"}
        got = self.extra(SUBSYNC_EXTRA_SOURCES="osv3").search("movie", "tt1", hints=hints)
        self.assertEqual([(c["id"], c["lang"], c["hash_match"]) for c in got],
                         [("osv3-101", "eng", True), ("osv3-102", "rus", False)])   # French left out
        path = self.fake.asked("/osv3/subtitles/")[0]
        self.assertEqual(path, "/osv3/subtitles/movie/tt1/videoHash=abcd1234abcd1234&videoSize=123456"
                               "&filename=Movie.2010.mkv.json")


class GestdownTest(SourcesBase):
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


class SubSourceTest(SourcesBase):
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
        ex = self.extra(SUBSYNC_EXTRA_SOURCES="", SUBSYNC_SUBSOURCE_KEY="ss-key", ledger=ledger)
        ex.search("series", "tt2:1:2")
        self.assertEqual(ledger.used("subsource", "request", 86400), 2)
        ledger.count("subsource", "request", 58)                      # 60 a minute
        ex.cache.clear()
        n = len(self.fake.asked("/subsource"))
        self.assertEqual(ex.search("series", "tt2:1:2"), [])
        self.assertEqual(len(self.fake.asked("/subsource")), n)


class WaitPauseCacheTest(SourcesBase):
    def test_a_slow_source_is_left_out_and_its_late_answer_kept_for_next_time(self):
        self.fake.mode["tsdb"] = "slow"
        self.fake.slow_s = 1.2
        ex = self.extra(clock=time.time, SUBSYNC_EXTRA_SOURCES="tsdb,osv3", SUBSYNC_SOURCE_TIMEOUT_S="0.4")
        t0 = time.time()
        got = ex.search("movie", "tt1")
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual({c["source"] for c in got}, {"osv3"})
        time.sleep(1.5)                                              # the slow answer arrives in the background
        n = len(self.fake.asked("/tsdb"))
        got = ex.search("movie", "tt1")
        self.assertEqual({c["source"] for c in got}, {"osv3", "tsdb"})
        self.assertEqual(len(self.fake.asked("/tsdb")), n)           # from the cache

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

    def test_a_source_the_config_already_has_is_skipped(self):
        self.extra(SUBSYNC_EXTRA_SOURCES="tsdb,osv3").search("movie", "tt1", skip={"osv3"})
        self.assertFalse(self.fake.asked("/osv3"))
        self.assertTrue(self.fake.asked("/tsdb"))


class StateTest(unittest.TestCase):
    def test_ledger_windows_and_fair_share(self):
        clock = Clock()
        ledger = Ledger(clock=clock, keyed={"subdl": True})
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
