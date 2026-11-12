"""in the worker: a source the config already has is asked once; the same subtitle from two places counts
once; a site with a tight limit gives a file only a few subtitles, and near its limit only the likeliest; downloads carry
a source's key in a header and are counted. No network."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import RangeServer, local_network_only  # noqa: E402
from test_io import FakeIndex, realistic, srt_of  # noqa: E402
from subsync import choose, srclimits  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.sourcestate import Ledger  # noqa: E402
from subsync.store import Store  # noqa: E402
from subsync.subs import fingerprint  # noqa: E402
from subsync.worker import Job, Worker  # noqa: E402

RU = "1\n00:00:01,000 --> 00:00:02,000\nПривет\n\n2\n00:00:03,500 --> 00:00:04,000\nКак дела?\n"


class FingerprintTest(unittest.TestCase):
    def test_the_same_subtitle_in_another_encoding_line_ending_or_with_a_credit_line(self):
        a = fingerprint(RU)
        b = fingerprint(("﻿" + RU.replace("\n", "\r\n")))
        c = fingerprint(RU + "\n3\n00:00:05,000 --> 00:00:06,000\nSubtitles downloaded from www.example.org\n")
        d = fingerprint(RU.encode("cp1251").decode("cp1251"))
        self.assertEqual(len({a, b, c, d}), 1)

    def test_the_same_subtitle_through_opensubtitles_v3_plus(self):
        #'s syncs: V3+ serves WebVTT with a ">>OpenSubtitles V3+<<" line first and the first cue's lines marked
        # "NOTE"; the same subtitle from Stremio's v3 was listed a second time
        cues = [f"{i + 1}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},800\nLine number {i}\n"
                for i in range(40)]
        plain = "\n".join(cues)
        v3plus = ("WEBVTT\n\n00:00:00.000 --> 00:00:00.500\n>>OpenSubtitles V3+ v0.0.4<< =>show.s01e01.en.srt\n\n"
                  + "\n".join(c.replace(",", ".").replace("Line number 0", "NOTE Line number 0").split("\n", 1)[1]
                              for c in cues))
        self.assertEqual(fingerprint(plain), fingerprint(v3plus))

    def test_another_timing_or_text_is_another_subtitle(self):
        self.assertNotEqual(fingerprint(RU), fingerprint(RU.replace("00:00:03,500", "00:00:04,000")))
        self.assertNotEqual(fingerprint(RU), fingerprint(RU.replace("Привет", "Здравствуй")))


class FakeAios:
    def __init__(self, subs=(), sources=()):
        self.subs, self.config_sources, self.row = list(subs), set(sources), None

    def subtitles(self, *a):
        return [dict(c) for c in self.subs]

    def find_row(self, *a):
        return self.row


class FakeExtra:
    def __init__(self, found=(), ledger=None):
        self.found, self.calls, self.ledger = list(found), [], ledger

    def search(self, type_, video_id, langs=None, hints=None, skip=()):
        self.calls.append({"type": type_, "id": video_id, "langs": langs, "hints": dict(hints or {}), "skip": set(skip)})
        return [dict(c) for c in self.found]

    def headers_for(self, cand):
        return {"X-API-Key": "k"} if cand.get("source") == "subsource" else {}


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def worker(self, aios, extra, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng,rus"}, **env))
        return Worker(s, Store(self.dir), aios, None, extra=extra)

    def test_config_sources_are_tagged_skipped_and_their_twins_dropped(self):
        aios = FakeAios([{"id": "123", "url": "https://subs5.strem.io/en/download/file/123", "lang": "eng"},
                         {"id": "c1", "url": "https://my-addon.example/sub/1.srt", "lang": "eng"}], sources={"osv3"})
        extra = FakeExtra([{"id": "tsdb-1", "source": "tsdb", "url": "http://t/1", "lang": "eng"},
                           {"id": "x", "source": "osv3plus", "url": "https://my-addon.example/sub/1.srt", "lang": "eng"},
                           {"id": "123", "source": "osv3", "url": "https://other/123", "lang": "eng"}])
        w = self.worker(aios, extra)
        got = w._candidates(Job("movie", "tt1"), {"filename": "a.mkv", "infoHash": "a" * 40, "fileIdx": 0})
        self.assertEqual([(c["id"], c["source"], c.get("from_config", False)) for c in got],
                         [("123", "osv3", True), ("c1", "config", True), ("tsdb-1", "tsdb", False)])
        call = extra.calls[0]
        self.assertEqual(call["skip"], {"osv3"})
        self.assertEqual(call["langs"], ["eng", "rus"])
        self.assertEqual(call["hints"]["infoHash"], "a" * 40)
        extra.calls.clear()
        self.worker(aios, extra, SUBSYNC_SKIP_CONFIG_SOURCES="off")._candidates(Job("movie", "tt1"), {})
        self.assertEqual(extra.calls[0]["skip"], set())

    def test_the_listed_torrent_is_passed_to_the_sources(self):
        aios = FakeAios()
        aios.row = {"url": "http://127.0.0.1:9/never", "streamData": {"torrent": {"infoHash": "B" * 40, "fileIdx": 3}}}
        extra = FakeExtra()
        w = self.worker(aios, extra)
        w.process(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 20_000_000}))
        self.assertEqual((extra.calls[0]["hints"]["infoHash"], extra.calls[0]["hints"]["fileIdx"]), ("b" * 40, 3))


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def worker(self, ledger=None, who=None):
        s = Settings({"SUBSYNC_DATA": self.dir})
        w = Worker(s, Store(self.dir), FakeAios(), None, extra=FakeExtra(ledger=ledger))
        w.who = who
        return w

    def tries(self):
        cands = [{"id": f"subdl-{i}", "source": "subdl", "url": f"u{i}", "lang": "eng", "release": "X"} for i in range(5)]
        cands += [{"id": f"ss-{i}", "source": "subsource", "url": f"s{i}", "lang": "eng", "release": "X"} for i in range(6)]
        cands += [{"id": f"t-{i}", "source": "tsdb", "url": f"t{i}", "lang": "eng", "release": "X"} for i in range(7)]
        cands[4]["hash_match"] = True                                   # the fifth SubDL one names this file
        for c in cands:
            c["from_config"] = False                                    # no config subtitle: no "viewer's first"
        return list(enumerate(cands))

    def test_a_file_takes_only_a_few_from_a_site_with_a_tight_limit(self):
        kept = self.worker()._within_limits(self.tries(), "a.mkv", None, None)
        by = lambda src: [c["id"] for _, c in kept if c["source"] == src]   # noqa: E731
        self.assertEqual(len(by("subdl")), srclimits.PER_FILE_DOWNLOADS["subdl"])
        self.assertEqual(len(by("subsource")), srclimits.PER_FILE_DOWNLOADS["subsource"])
        self.assertEqual(len(by("tsdb")), 7)

    def test_near_the_limit_only_the_likeliest_and_at_the_limit_none(self):
        ledger = Ledger(keyed={"subdl": True})
        ledger.count("subdl", "download", 200, who="me")
        ledger.count("subdl", "download", 45, who="other")
        kept = self.worker(ledger, who="me")._within_limits(self.tries(), "a.mkv", None, None)
        self.assertEqual([c["id"] for _, c in kept if c["source"] == "subdl"], ["subdl-4"])   # the hash match only
        ledger.count("subdl", "download", 55)
        kept = self.worker(ledger, who="me")._within_limits(self.tries(), "a.mkv", None, None)
        self.assertFalse([c for _, c in kept if c["source"] == "subdl"])

    def test_a_built_in_subtitle_first_in_the_list_is_not_the_viewers_first(self):
        tries = self.tries()
        kept = self.worker()._within_limits(tries, "a.mkv", None, None)
        self.assertEqual([c["id"] for _, c in kept if c["source"] == "subdl"], ["subdl-0", "subdl-1", "subdl-2"])
        cands = [c for _, c in tries]
        self.assertEqual(choose.order(cands, viewer_first=Worker.viewer_first(cands))[0][1]["id"], "subdl-4")

    def test_tier_of_a_release_match(self):
        self.assertEqual(choose.tier_of(3, {"release_match": True, "release": "x"}, "a.mkv", None, None), 2)
        self.assertEqual(choose.tier_of(3, {"hash_match": True}, "a.mkv", None, None), 1)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.guard = local_network_only()
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append({k.lower(): v for k, v in self.headers.items()})
                body = RU.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_a_download_carries_the_sources_key_and_is_counted(self):
        ledger = Ledger(keyed={"subsource": True, "subdl": True})
        w = Worker(Settings({"SUBSYNC_DATA": self.dir}), Store(self.dir), FakeAios(), None, extra=FakeExtra(ledger=ledger))
        w.who = "me"
        got = w._fetch_one(Job("movie", "tt1"), "rus", 0, {"id": "ss-1", "source": "subsource", "url": self.url + "/a"})
        self.assertIsNotNone(got)
        self.assertEqual(self.seen[-1].get("x-api-key"), "k")
        self.assertEqual(ledger.used("subsource", "request", 86400), 1)        # SubSource counts a download as a request
        self.assertEqual(ledger.used("subsource", "request", 86400, "me"), 1)
        w._fetch_one(Job("movie", "tt1"), "rus", 0, {"id": "d-1", "source": "subdl", "url": self.url + "/b"})
        self.assertIsNone(self.seen[-1].get("x-api-key"))
        self.assertEqual(ledger.used("subdl", "download", 86400), 1)


class DuplicateTest(unittest.TestCase):
    """The same subtitle from two sources (here: another line ending, a byte-order mark and a credit line at the end)
    is judged and listed once."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        ref = realistic(200, 3)
        insync = srt_of(ref)
        n = len(ref) + 1
        copy = "﻿" + (insync + f"\n{n}\n00:59:00,000 --> 00:59:02,000\nSynced by someone, www.example.org\n"
                           ).replace("\n", "\r\n")
        for name, text in {"insync.srt": insync, "copy.srt": copy, "offset.srt": srt_of(ref, shift=2.7, jitter=0.05)}.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="") as f:
                f.write(text)
        self.srv = RangeServer(self.dir)
        self.addCleanup(self.srv.close)
        self.cands = [{"id": "a-insync", "url": self.srv.url("insync.srt"), "lang": "eng", "release": "Movie BluRay"},
                      {"id": "b-copy", "url": self.srv.url("copy.srt"), "lang": "eng", "release": "Movie BluRay"},
                      {"id": "c-offset", "url": self.srv.url("offset.srt"), "lang": "eng", "release": "Movie DVDRip"}]
        self.idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                                    "cues": ref}])

    def test_judged_and_listed_once(self):
        store = Store(os.path.join(self.dir, "store"))
        s = Settings({"SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_BEST_FIT": "on", "SUBSYNC_FAST_PATH": "on",
                      "SUBSYNC_LIST_IN_SYNC": "on", "SUBSYNC_PARALLEL_LANGUAGES": "off", "SUBSYNC_IMAGE_REF": "off",
                      "SUBSYNC_ALASS": "/nonexistent/alass"})
        w = Worker(s, store, None, None)
        w._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", self.cands, "abcd", "Movie.mkv")
        tried = {x[0] for x in store.db.execute("SELECT candidate_id FROM results")}
        self.assertEqual(len(tried & {"a-insync", "b-copy"}), 1)
        offered = [r["candidate_id"] for r in store.offers("f-1", ["eng"], {"eng": 3})]
        self.assertEqual(len([c for c in offered if c in ("a-insync", "b-copy")]), 1)


if __name__ == "__main__":
    unittest.main()
