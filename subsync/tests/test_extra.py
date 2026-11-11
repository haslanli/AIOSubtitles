"""Subtitle sources subsync searches itself, besides what the user's AIOStreams lists. A local stand-in
answers like TheSubtitleDB, Cinemeta + Gestdown, SubDL and Wyzie; no network."""
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

from subsync import choose  # noqa: E402
from subsync.extra import ExtraSources  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.sources import fetch_subtitle  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n"


def zipped(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, SRT.replace("Hello", n))
    return buf.getvalue()


class Fake:
    def __init__(self):
        self.calls, self.fail = [], set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json"):
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                outer.calls.append((u.path, q))
                top = u.path.split("/")[1]
                if top in outer.fail:
                    return self.send(500, {"error": "down"})
                if u.path.startswith("/tsdb/v1/by-imdb/tt1"):
                    items = [{"id": 11, "language": "en", "format": "srt", "release_name": "Movie.2010.WEB-DL", "cues": 900,
                              "duration_s": 7000, "hearing_impaired": False, "ai": None, "ai_transcribed": False,
                              "download_url": f"{outer.url}/tsdb/get/11"},
                             {"id": 12, "language": "ru", "format": "srt", "release_name": "Movie.2010.BluRay", "cues": 870,
                              "hearing_impaired": False, "ai": True, "ai_transcribed": False,
                              "download_url": f"{outer.url}/tsdb/get/12"},
                             {"id": 13, "language": "en", "format": "srt", "release_name": "Movie.2010.HDTV", "cues": 1500,
                              "hearing_impaired": True, "ai": None, "ai_transcribed": False,
                              "download_url": f"{outer.url}/tsdb/get/13"}]
                    off = int(q.get("offset", 0))
                    page = items[off:off + 2]
                    return self.send(200, {"subtitles": {"total": 3, "limit": 2, "offset": off, "items": page}})
                if u.path.startswith("/tsdb/get/"):
                    return self.send(200, SRT.encode(), "text/plain")
                if u.path == "/cinemeta/meta/series/tt2.json":
                    return self.send(200, {"meta": {"name": "Show", "tvdb_id": 121361}})
                if u.path == "/gestdown/shows/external/tvdb/121361":
                    return self.send(200, {"shows": [{"id": "S1", "name": "Show"}]})
                if u.path.startswith("/gestdown/subtitles/get/S1/1/2/"):
                    lang = u.path.rsplit("/", 1)[1]
                    subs = [{"subtitleId": f"G-{lang}", "version": "WEB.NTb", "completed": True, "hearingImpaired": False,
                             "downloadUri": f"/subtitles/download/G-{lang}"},
                            {"subtitleId": "G-half", "version": "x", "completed": False, "hearingImpaired": False,
                             "downloadUri": "/subtitles/download/G-half"}] if lang == "English" else []
                    return self.send(200, {"matchingSubtitles": subs})
                if u.path.startswith("/gestdown/subtitles/download/"):
                    return self.send(200, SRT.encode(), "text/plain")
                if u.path == "/subdl/api/v1/subtitles":
                    return self.send(200, {"status": True, "subtitles": [
                        {"release_name": "Show.S01E02.1080p.WEB", "lang": "english", "language": "EN",
                         "url": "/subtitle/1-2.zip", "hi": 0}]})
                if u.path == "/subdl-dl/subtitle/1-2.zip":
                    return self.send(200, zipped(["Show.S01E01.srt", "Show.S01E02.srt", "readme.txt"]), "application/zip")
                if u.path == "/wyzie/search":
                    return self.send(200, [{"id": "w1", "url": f"{outer.url}/wyzie/dl/w1", "language": q.get("language"),
                                            "display": "English", "isHearingImpaired": False, "format": "srt",
                                            "source": "subf2m", "release": "Movie.2010.1080p"}])
                if u.path.startswith("/wyzie/dl/"):
                    return self.send(200, SRT.encode(), "text/plain")
                if u.path.startswith("/osv3/") and "/subtitles/" in u.path:
                    return self.send(200, {"subtitles": [
                        {"id": "v3+|1|Movie.2010.WEB-DL", "sub_id": 1, "ai_translated": False, "from_trusted": True,
                         "lang_code": "en", "lang": "eng", "title": "Movie.2010.WEB-DL", "moviehash": "abcd1234abcd1234",
                         "url": f"{outer.url}/osv3/sub.vtt?id=1"},
                        {"id": "v3+|2|Movie.2010.BluRay", "sub_id": 2, "ai_translated": True, "from_trusted": False,
                         "lang_code": "ru", "lang": "rus", "title": "Movie.2010.BluRay", "moviehash": None,
                         "url": f"{outer.url}/osv3/sub.vtt?id=2"},
                        {"id": "v3+|3|Movie", "lang": "fre", "title": "Movie", "url": f"{outer.url}/osv3/sub.vtt?id=3"}]})
                if u.path == "/osv3/sub.vtt":
                    return self.send(200, SRT.encode(), "text/plain")
                return self.send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ExtraSourcesTest(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        u = self.fake.url
        self.env = {"SUBSYNC_LANGUAGES": "eng,rus", "SUBSYNC_TSDB_URL": f"{u}/tsdb", "SUBSYNC_CINEMETA_URL": f"{u}/cinemeta",
                    "SUBSYNC_GESTDOWN_URL": f"{u}/gestdown", "SUBSYNC_SUBDL_URL": f"{u}/subdl",
                    "SUBSYNC_SUBDL_DOWNLOAD_URL": f"{u}/subdl-dl", "SUBSYNC_WYZIE_URL": f"{u}/wyzie",
                    "SUBSYNC_OSV3PLUS_URL": f"{u}/osv3"}

    def tearDown(self):
        self.fake.close()

    def extra(self, **env):
        return ExtraSources(Settings(dict(self.env, **env)))

    def test_defaults(self):
        s = Settings({})
        self.assertEqual(s.extra_sources, ["tsdb", "gestdown", "osv3plus"])   # the keyless ones
        self.assertEqual(s.osv3plus_url, "https://opensubtitles.stremio.homes")   # AIOStreams' own preset address
        self.assertEqual((s.subdl_key, s.wyzie_key), ("", ""))
        self.assertEqual(s.gate_tries, 20)

    def test_thesubtitledb_pages_through_and_keeps_what_it_says_about_each_subtitle(self):
        got = self.extra().search("movie", "tt1")
        tsdb = [c for c in got if c["source"] == "tsdb"]
        self.assertEqual([(c["id"], c["lang"], c["cues"]) for c in tsdb],
                         [("tsdb-11", "eng", 900), ("tsdb-12", "rus", 870), ("tsdb-13", "eng", 1500)])
        self.assertTrue(tsdb[1]["ai"] and tsdb[2]["hi"])
        self.assertEqual(tsdb[0]["release"], "Movie.2010.WEB-DL")
        pages = [q for p, q in self.fake.calls if p.startswith("/tsdb/v1")]
        self.assertEqual([q.get("offset") for q in pages], ["0", "2"])
        self.assertEqual(pages[0]["lang"], "en,ru")
        self.assertIn("Hello", fetch_subtitle(tsdb[0]["url"]).decode())

    def test_gestdown_is_asked_for_series_only(self):
        got = self.extra().search("series", "tt2:1:2")
        gd = [c for c in got if c["source"] == "gestdown"]
        self.assertEqual([(c["id"], c["lang"], c["release"]) for c in gd], [("gd-G-English", "eng", "WEB.NTb")])
        self.assertTrue(any(p == "/gestdown/subtitles/get/S1/1/2/Russian" for p, _ in self.fake.calls))
        self.fake.calls.clear()
        self.extra().search("movie", "tt1")
        self.assertFalse(any(p.startswith(("/gestdown", "/cinemeta")) for p, _ in self.fake.calls))

    def test_subdl_and_wyzie_need_a_key(self):
        got = self.extra().search("series", "tt2:1:2")
        self.assertFalse({c["source"] for c in got} & {"subdl", "wyzie"})    # no key: neither SubDL nor Wyzie is asked
        self.assertFalse(any(p.startswith(("/subdl", "/wyzie")) for p, _ in self.fake.calls))
        self.fake.calls.clear()
        got = self.extra(SUBSYNC_SUBDL_KEY="k1", SUBSYNC_WYZIE_KEY="k2").search("series", "tt2:1:2")
        subdl = next(c for c in got if c["source"] == "subdl")
        self.assertEqual(subdl["lang"], "eng")
        self.assertIn("Show.S01E02.srt", fetch_subtitle(subdl["url"]).decode())   # the zip's file for this episode
        q = next(q for p, q in self.fake.calls if p == "/subdl/api/v1/subtitles")
        self.assertEqual((q["imdb_id"], q["type"], q["season_number"], q["episode_number"], q["api_key"]),
                         ("tt2", "tv", "1", "2", "k1"))
        wy = [c for c in got if c["source"] == "wyzie"]
        self.assertEqual(sorted(c["lang"] for c in wy), ["eng", "rus"])
        self.assertTrue(all(q.get("key") == "k2" for p, q in self.fake.calls if p == "/wyzie/search"))

    def test_opensubtitles_v3_plus_is_asked_with_the_files_hash_size_and_name(self):
        # Asked like Stremio and AutoSync ask it, with the languages and options in the path as AIOStreams' preset
        # builds them; AI-translated ones left out while SUBSYNC_SKIP_AI is on
        hints = {"filename": "Movie 2010 [x].mkv", "videoSize": 123456, "videoHash": "abcd1234abcd1234"}
        got = self.extra(SUBSYNC_EXTRA_SOURCES="osv3plus").search("movie", "tt1", hints=hints)
        self.assertEqual([(c["id"], c["source"], c["lang"], c["release"], c["hash"], c["ai"]) for c in got],
                         [("v3+|1|Movie.2010.WEB-DL", "osv3plus", "eng", "Movie.2010.WEB-DL", "abcd1234abcd1234", False),
                          ("v3+|2|Movie.2010.BluRay", "osv3plus", "rus", "Movie.2010.BluRay", "", True)])   # French dropped
        path = next(p for p, _ in self.fake.calls if "/subtitles/" in p)
        self.assertEqual(path, "/osv3/en%7Cru/ai-translated%3Dfalse%7Cfrom%3Dall%7Cauto-adjustment%3Dfalse"
                               "/subtitles/movie/tt1/videoHash=abcd1234abcd1234&videoSize=123456&filename=Movie%202010%20%5Bx%5D.mkv.json")
        self.assertIn("Hello", fetch_subtitle(got[0]["url"]).decode())
        self.fake.calls.clear()
        self.extra(SUBSYNC_EXTRA_SOURCES="osv3plus", SUBSYNC_SKIP_AI="off").search("series", "tt2:1:2")
        path = next(p for p, _ in self.fake.calls if "/subtitles/" in p)
        self.assertEqual(path, "/osv3/en%7Cru/ai-translated%3Dtrue%7Cfrom%3Dall%7Cauto-adjustment%3Dfalse"
                               "/subtitles/series/tt2:1:2.json")                # no hints: the id alone

    def test_the_worker_passes_the_files_hints(self):
        from subsync.store import Store
        from subsync.worker import Job, Safety, Worker

        class Aios:
            def find_row(self, *a):
                return {"url": "http://127.0.0.1:9/never"}

            def subtitles(self, *a):
                return []

        class Extra:
            calls = []

            def search(self, *a, **k):
                self.calls.append((a, k))
                return []
        import tempfile
        d = tempfile.mkdtemp(prefix="subsync-test-")
        extra = Extra()
        w = Worker(Settings({"SUBSYNC_DATA": d}), Store(d), Aios(), Safety(), extra=extra)
        w.process(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 5, "videoHash": "abcd"}))
        (args, kw), = extra.calls
        self.assertEqual(args[:2], ("movie", "tt1"))
        self.assertEqual(kw.get("hints"), {"filename": "a.mkv", "videoSize": 5, "videoHash": "abcd"})

    def test_a_failing_source_does_not_stop_the_others(self):
        self.fake.fail.add("tsdb")
        got = self.extra().search("series", "tt2:1:2")
        self.assertEqual({c["source"] for c in got}, {"gestdown", "osv3plus"})

    def test_off_means_nothing_is_asked(self):
        self.assertEqual(self.extra(SUBSYNC_EXTRA_SOURCES="").search("series", "tt2:1:2"), [])
        self.assertEqual(self.fake.calls, [])


class PacingTest(unittest.TestCase):
    """OpenSubtitles refuses bursts with HTTP 429 (8 at once: 9 of 40 refused). Its downloads go at most 2 at a
    time, and a 429 is tried again after a short wait."""

    def setUp(self):
        from subsync import sources
        self.sources = sources
        self.saved = sources.PACED_HOSTS
        sources.PACED_HOSTS = ("127.0.0.1",)            # the stand-in plays OpenSubtitles here
        self.state = {"now": 0, "max": 0, "hits": {}}
        lock, st = threading.Lock(), self.state

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with lock:
                    st["now"] += 1
                    st["max"] = max(st["max"], st["now"])
                    st["hits"][self.path] = st["hits"].get(self.path, 0) + 1
                    first = st["hits"][self.path] == 1
                try:
                    time.sleep(0.15)
                    if self.path.startswith("/busy") and first:
                        self.send_response(429)
                        self.send_header("Retry-After", "1")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = SRT.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with lock:
                        st["now"] -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.sources.PACED_HOSTS = self.saved
        self.server.shutdown()
        self.server.server_close()

    def test_at_most_two_at_a_time(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as p:
            got = list(p.map(lambda i: fetch_subtitle(f"{self.url}/sub/{i}"), range(8)))
        self.assertTrue(all(b"Hello" in g for g in got))
        self.assertLessEqual(self.state["max"], 2)

    def test_a_429_is_tried_again(self):
        t0 = time.time()
        self.assertIn(b"Hello", fetch_subtitle(f"{self.url}/busy/1"))
        self.assertEqual(self.state["hits"]["/busy/1"], 2)
        self.assertGreaterEqual(time.time() - t0, 0.9)        # it waited for Retry-After

    def test_other_hosts_are_not_paced(self):
        self.sources.PACED_HOSTS = ("opensubtitles", "strem.io")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as p:
            list(p.map(lambda i: fetch_subtitle(f"{self.url}/sub/{i}"), range(6)))
        self.assertGreater(self.state["max"], 2)


class CueCountTest(unittest.TestCase):
    """A candidate whose line count is within 2 % of the release's own track is likely made from that release: tried
    right after the viewer's first, like a hash match."""

    def test_matching_line_count_comes_early(self):
        cands = [{"release": "A"}, {"release": "B", "cues": 1500}, {"release": "C", "cues": 905}, {"release": "D"}]
        self.assertEqual([r for r, _ in choose.order(cands, None, None, ref_cues=900)], [0, 2, 1, 3])
        self.assertEqual([r for r, _ in choose.order(cands, None, None)], [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
