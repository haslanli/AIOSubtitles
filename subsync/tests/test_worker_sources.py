"""in the worker: a source the config already has is asked once; the same subtitle from two places counts
once; a site with a tight limit gives a file only a few subtitles, and near its limit only the likeliest; downloads carry
a source's key in a header and are counted. No network."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import (closed_after, dialogue_srt, FakeDownloads, FakeIndex, local_network_only,  # noqa: E402
                     RangeServer, realistic, srt_of, start_server, stop_server)
from subsync import choose, pipeline, srclimits  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.sourcestate import Ledger  # noqa: E402
from subsync.store import Store  # noqa: E402
from subsync.subs import decode, fingerprint  # noqa: E402
from subsync.worker import Job, Worker  # noqa: E402

RU = "1\n00:00:01,000 --> 00:00:02,000\nПривет\n\n2\n00:00:03,500 --> 00:00:04,000\nКак дела?\n"


class FingerprintTest(unittest.TestCase):
    def test_the_same_subtitle_in_another_encoding_line_ending_or_with_a_credit_line(self):
        a = fingerprint(RU)
        b = fingerprint(("﻿" + RU.replace("\n", "\r\n")))
        c = fingerprint(RU + "\n3\n00:00:05,000 --> 00:00:06,000\nSubtitles downloaded from www.example.org\n")
        # another encoding: its bytes decoded as the worker decodes a download (the test round-tripped the
        # text itself, so decode() was never part of it)
        d = fingerprint(decode(RU.encode("cp1251"), "rus"))
        self.assertEqual(len({a, b, c, d}), 1)

    def test_the_same_subtitle_through_opensubtitles_v3_plus(self):
        # seen in real syncs: V3+ serves WebVTT with a ">>OpenSubtitles V3+<<" line first and the first cue's
        # lines marked "NOTE"; the same subtitle from Stremio's v3 was listed a second time
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

    def test_with_no_edge_left_out_a_longer_subtitle_is_still_told_apart(self):
        long = "\n".join(f"{i + 1}\n00:00:{i:02d},000 --> 00:00:{i:02d},800\nLine {i}\n" for i in range(30))
        with mock.patch.object(srclimits, "FINGERPRINT_EDGE", 0):   # lines[0:-0] was no line at all
            self.assertNotEqual(fingerprint(long), fingerprint(long.replace("Line 15", "Another line")))


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
        return Worker(s, closed_after(self, Store(self.dir)), aios, None, extra=extra)

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
        w = Worker(s, closed_after(self, Store(self.dir)), FakeAios(), None, extra=FakeExtra(ledger=ledger))
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
        closed_after(self, ledger.db)                        # its database in memory
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

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.addCleanup(stop_server, self.server)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_a_download_carries_the_sources_key_and_is_counted(self):
        ledger = Ledger(keyed={"subsource": True, "subdl": True})
        closed_after(self, ledger.db)                        # its database in memory
        w = Worker(Settings({"SUBSYNC_DATA": self.dir}), closed_after(self, Store(self.dir)), FakeAios(), None,
                   extra=FakeExtra(ledger=ledger))
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
        store = closed_after(self, Store(os.path.join(self.dir, "store")))
        s = Settings({"SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_BEST_FIT": "on", "SUBSYNC_FAST_PATH": "on",
                      "SUBSYNC_LIST_IN_SYNC": "on", "SUBSYNC_PARALLEL_LANGUAGES": "off", "SUBSYNC_IMAGE_REF": "off",
                      "SUBSYNC_ALASS": "/nonexistent/alass"})
        w = Worker(s, store, None, None)
        w._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", self.cands, "abcd", "Movie.mkv")
        tried = {x[0] for x in store.db.execute("SELECT candidate_id FROM results")}
        self.assertEqual(len(tried & {"a-insync", "b-copy"}), 1)
        offered = [r["candidate_id"] for r in store.offers("f-1", ["eng"], {"eng": 3})]
        self.assertEqual(len([c for c in offered if c in ("a-insync", "b-copy")]), 1)


    def test_a_second_download_with_no_dialogue_line_is_left_out_as_such(self):
        # R3-8: every text with no dialogue line (an error page, a broken download) has one fingerprint, so a
        # second one is left out as before, but the log said it was the same subtitle as one already judged
        for name, page in (("gone.srt", "<html><body>404 Not Found</body></html>"),
                           ("busy.srt", "<html><body>429 Too Many Requests</body></html>")):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(page)
        cands = [self.cands[0]] + [{"id": f"e-{n}", "url": self.srv.url(n), "lang": "eng", "release": "Movie BluRay"}
                                   for n in ("gone.srt", "busy.srt")]
        store = closed_after(self, Store(os.path.join(self.dir, "store")))
        s = Settings({"SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_BEST_FIT": "on", "SUBSYNC_FAST_PATH": "on",
                      "SUBSYNC_PARALLEL_LANGUAGES": "off", "SUBSYNC_IMAGE_REF": "off",
                      "SUBSYNC_ALASS": "/nonexistent/alass"})
        with self.assertLogs("subsync.worker", "INFO") as logs:
            Worker(s, store, None, None)._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", cands, "abcd",
                                                         "Movie.mkv")
        left_out = [m for m in logs.output if m.endswith(", left out")]   # the later of the two to download
        self.assertEqual(len(left_out), 1, logs.output)
        self.assertRegex(left_out[0], r"^INFO:subsync\.worker:sync movie/tt1 eng rank [12]: "
                                      r"no dialogue line, left out$")


class LanguageCheckWorkerTest(unittest.TestCase):
    """A downloaded subtitle whose text isn't in its language is recorded as kept ("not <Language> text"), never
    judged, offered or anchored on; an original chosen by name before any download is replaced once its text is
    refused; with nothing passing, the original kept for the "(original)" entry is the one measured best as it is,
    else the likeliest by name, and never one in another language. Downloads are fakes: no network."""
    URLS = {"os-es": ("spa", 0), "os-en": ("eng", 0), "os-en2": ("eng", 1), "os-ru": ("rus", 0), "os-enru": ("eng", 0)}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.texts = {f"http://subs.test/{cid}.srt": dialogue_srt(lang, start=30.0 + shift)
                      for cid, (lang, shift) in self.URLS.items()}
        self.downloads = FakeDownloads(self.texts)
        for patcher in (self.downloads.patch(), local_network_only()):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = closed_after(self, Store(os.path.join(self.dir, "store")))

    def worker(self, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_BEST_FIT": "on",
                           "SUBSYNC_FAST_PATH": "on", "SUBSYNC_LIST_IN_SYNC": "on", "SUBSYNC_PARALLEL_LANGUAGES": "off",
                           "SUBSYNC_IMAGE_REF": "off", "SUBSYNC_ALASS": "/nonexistent/alass", "SUBSYNC_EXTRA_SOURCES": "",
                           "SUBSYNC_LANGUAGES": "eng"}, **env))
        return Worker(s, self.store, FakeAios(), None)

    @staticmethod
    def cand(cid, lang="eng", release=""):
        return {"id": cid, "url": f"http://subs.test/{cid}.srt", "lang": lang, "release": release}

    def rows(self):
        return {cid: (outcome, reason, json.loads(detail)) for cid, outcome, reason, detail in self.store.db.execute(
            "SELECT candidate_id, outcome, reason, detail FROM results")}

    def judged(self, measures=None):
        """The pipeline as a stub: no original fits as it is; each fit keeps the original, measured as `measures` says
        by the text's first line (None: not measured). Returns the texts it was given."""
        seen = []

        def sync_subtitle(index, text, lang, **kw):
            seen.append(text)
            first = text.split("\n")[2]
            within = (measures or {}).get(first)
            out = {"outcome": "keep_original", "reason": "below the bar", "route": "cues"}
            if within is not None:
                out["gate_original"] = {"within200": within, "median_ms": 300}
            return out
        for name, fake in (("check_original", lambda *a, **k: None), ("shift_sync", lambda *a, **k: None),
                           ("sync_subtitle", sync_subtitle)):
            patcher = mock.patch.object(pipeline, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return seen

    def in_sync(self):
        """The pipeline as a stub: every original is in sync as it is."""
        patcher = mock.patch.object(pipeline, "check_original", lambda *a, **k: {
            "in_sync": True, "route": "cues", "gate_original": {"within200": 0.98, "median_ms": 20}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, w, cands, lang="eng", filename="Movie.mkv"):
        return w._sync_language(Job("movie", "tt1"), FakeIndex(), "f-1", lang, cands, "abcd", filename)

    def test_a_subtitle_in_another_language_is_kept_and_never_judged_or_offered(self):
        self.in_sync()
        w = self.worker()
        published = []
        w._publish = lambda alias, lang, key, final=False: published.append(key)
        key = self.sync(w, [self.cand("os-es"), self.cand("os-en")])     # the Spanish one is the viewer's first
        self.assertEqual(self.store.get(key)["candidate_id"], "os-en")
        rows = self.rows()
        self.assertEqual(rows["os-es"][:2], ("keep_original", "not English text"))
        self.assertNotIn("gate_original", rows["os-es"][2])               # never judged
        self.assertEqual(rows["os-es"][2]["language"]["verdict"], "fail")
        self.assertEqual([r["candidate_id"] for r in self.store.offers("f-1", ["eng"], {"eng": 3})], ["os-en"])
        self.assertEqual({self.store.get(k)["candidate_id"] for k in published}, {"os-en"})

    def test_a_refused_original_is_replaced_by_the_next_by_name_whose_text_passes(self):
        self.in_sync()
        w = self.worker()
        alias = Job("movie", "tt1").alias
        cands = [self.cand("os-es", release="Movie BluRay"), self.cand("os-en", release="Movie BluRay")]
        w._set_originals(alias, cands, "Movie.mkv", None)
        self.assertEqual(w.known_originals(alias)["eng"]["id"], "os-es")    # chosen by name, before any download
        self.sync(w, cands)
        self.assertEqual(w.known_originals(alias)["eng"], {"url": "http://subs.test/os-en.srt", "id": "os-en"})
        self.assertEqual(self.store.originals_get(alias)["eng"]["id"], "os-en")   # kept across a restart too
        w._set_originals(alias, cands[:1], "Movie.mkv", None)
        self.sync(w, cands[:1])                                            # none left in the language: none
        self.assertIsNone(w.known_originals(alias)["eng"])

    def test_nothing_passing_keeps_the_best_measured_original_in_its_language(self):
        # os-en2 gets a first line of its own, so the stub measures it apart from os-en
        self.downloads.files["http://subs.test/os-en2.srt"] = dialogue_srt("eng", start=31.0).replace(
            "I don't know what you want from me.", "I don't know what you want.", 1).encode()
        seen = self.judged({"No sé qué quieres de mí.": 0.95, "I don't know what you want from me.": 0.40,
                            "I don't know what you want.": 0.62})
        w = self.worker()
        self.assertIsNone(self.sync(w, [self.cand("os-es"), self.cand("os-en"), self.cand("os-en2")]))
        kept = self.store.kept_original("f-1", "eng")
        self.assertEqual((kept["candidate_id"], kept["measure"]), ("os-en2", 0.62))
        self.assertEqual(self.rows()["os-es"][1], "not English text")
        self.assertFalse(any("No sé qué" in text for text in seen))         # the Spanish one was never judged

    def test_with_nothing_measured_the_likeliest_by_name_in_its_language(self):
        self.judged()
        w = self.worker()
        group = "Movie.2001.BluRay-GRP"
        cands = [self.cand("os-en2"), self.cand("os-es", release=group), self.cand("os-en", release=group)]
        self.assertIsNone(self.sync(w, cands, filename="Movie.2001.BluRay-GRP.mkv"))
        kept = self.store.kept_original("f-1", "eng")
        self.assertEqual((kept["candidate_id"], kept["measure"]), ("os-en", None))   # os-es names the group first
        self.sync(w, [self.cand("os-es")])
        self.assertIsNone(self.store.kept_original("f-1", "eng")["candidate_id"])  # every one refused: none
        self.sync(w, [], lang="rus")
        self.assertIsNone(self.store.kept_original("f-1", "rus"))           # no subtitle at all: nothing kept

    def test_a_waiting_file_never_anchors_on_another_language(self):
        w = self.worker(SUBSYNC_LANGUAGES="eng,rus", SUBSYNC_AUDIO_ROUTE="on")
        job = Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
        per_lang = {"eng": [self.cand("os-es"), self.cand("os-en")],
                    "rus": [self.cand("os-enru", "rus"), self.cand("os-ru", "rus")]}
        w._set_originals(job.alias, per_lang["eng"] + per_lang["rus"], "b.mp4", None)
        self.assertEqual(w.known_originals(job.alias)["eng"]["id"], "os-es")
        w._await_pick(job, per_lang, "b.mp4", None)
        originals = w.known_originals(job.alias)
        self.assertEqual((originals["eng"]["id"], originals["rus"]["id"]), ("os-en", "os-ru"))
        anchored = w.provisional_for(job.alias, "rus")
        self.assertIsNotNone(anchored)
        self.assertIn("Я не знаю", anchored)                                  # the Russian text, not the English one
        self.assertIn("http://subs.test/os-en.srt", self.downloads.asked)    # the next by name, downloaded to check it

    def test_a_failed_download_alone_keeps_the_original(self):
        # not a refusal: the language keeps its entry, and its original is downloaded again when served
        w = self.worker(SUBSYNC_LANGUAGES="eng,rus", SUBSYNC_AUDIO_ROUTE="on")
        job = Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
        per_lang = {"eng": [self.cand("os-en")], "rus": [self.cand("os-gone", "rus")]}
        w._set_originals(job.alias, per_lang["eng"] + per_lang["rus"], "b.mp4", None)
        w._await_pick(job, per_lang, "b.mp4", None)
        self.assertEqual(w.known_originals(job.alias)["rus"]["id"], "os-gone")


class SettledLanguageTest(unittest.TestCase):
    """D1: a language is settled, so the track's words are no longer read to confirm its later
    subtitles, only by a pass against a track in its own language. The pipeline is a stub; downloads are fakes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        texts = {f"http://subs.test/{cid}.srt": dialogue_srt("spa", start=start)
                 for cid, start in (("os-a", 30.0), ("os-b", 31.0))}
        for patcher in (FakeDownloads(texts).patch(), local_network_only()):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = closed_after(self, Store(os.path.join(self.dir, "store")))

    def unsettled(self, track_language):
        """Spanish subtitle A passes the main rule against a track in `track_language`; then B is re-timed. Whether
        each was re-timed with the language unsettled."""
        seen = {}

        def sync_subtitle(index, text, lang, **kw):
            first = "00:00:30,000" in text.split("\n")[1]
            seen["a" if first else "b"] = kw["unsettled"]
            if not first:
                return {"outcome": "keep_original", "reason": "below the bar", "route": "cues"}
            return {"outcome": "synced", "reason": "passes against the release track (snapped)", "route": "cues",
                    "reference": {"track": 3, "language": track_language, "codec": "S_TEXT/UTF8", "cues": 1200},
                    "body": text}
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_BEST_FIT": "on",
                      "SUBSYNC_FAST_PATH": "off", "SUBSYNC_RANK_BY_RELEASE": "off", "SUBSYNC_PARALLEL_LANGUAGES": "off",
                      "SUBSYNC_IMAGE_REF": "off", "SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_LANGUAGES": "spa"})
        w = Worker(s, self.store, FakeAios(), None)
        w._publish = lambda *a, **k: None
        cands = [{"id": cid, "url": f"http://subs.test/{cid}.srt", "lang": "spa", "release": ""} for cid in ("os-a", "os-b")]
        with mock.patch.object(pipeline, "sync_subtitle", sync_subtitle):
            w._sync_language(Job("movie", "tt1"), FakeIndex(), f"f-{track_language}", "spa", cands, "abcd", "Movie.mkv")
        return seen

    def test_a_pass_against_another_languages_track_leaves_it_unsettled(self):
        self.assertEqual(self.unsettled("en"), {"a": True, "b": True})      # before: B re-timed with Spanish settled
        self.assertEqual(self.unsettled("es"), {"a": True, "b": False})


class LoggedDownloads(FakeDownloads):
    """FakeDownloads that logs when each download starts and ends; `on_start(url)` is called as one starts."""

    def __init__(self, files):
        super().__init__(files)
        self.log, self.lock, self.on_start = [], threading.Lock(), lambda url: None

    def __call__(self, url, timeout=30, headers=None):
        with self.lock:
            self.log.append(("start", url))
        self.on_start(url)
        try:
            return super().__call__(url, timeout, headers)
        finally:
            with self.lock:
                self.log.append(("end", url))


class PickedLanguageTest(unittest.TestCase):
    """A pick for a language of a file whose job is queued or running gets alass
    at once on the first in try order of that language's first checked downloads when none of them fits as it is or by
    one shift; it no longer holds the other languages' downloads back. Downloads and the pipeline are fakes (no
    network, no alass); held downloads are released by the fakes themselves, so nothing sleeps."""
    LANGS = ("eng", "rus", "spa")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.cands = {lang: [{"id": f"{lang}-{i}", "url": f"http://subs.test/{lang}-{i}.srt", "lang": lang,
                              "release": ""} for i in range(8)] for lang in self.LANGS}
        texts = {c["url"]: dialogue_srt(c["lang"], start=30.0 + i)
                 for cs in self.cands.values() for i, c in enumerate(cs)}
        self.ids = {(c["lang"], texts[c["url"]].split("\n")[1]): c["id"] for cs in self.cands.values() for c in cs}
        self.downloads = LoggedDownloads(texts)
        for patcher in (self.downloads.patch(), local_network_only()):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = closed_after(self, Store(os.path.join(self.dir, "store")))
        self.events, self.lock = [], threading.Lock()
        self.on_check = lambda cid, n: None

    def worker(self, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_BEST_FIT": "on",
                           "SUBSYNC_FAST_PATH": "on", "SUBSYNC_SHIFT_PATH": "on", "SUBSYNC_LIST_IN_SYNC": "on",
                           "SUBSYNC_PARALLEL_LANGUAGES": "on", "SUBSYNC_IMAGE_REF": "off",
                           "SUBSYNC_ALASS": "/nonexistent/alass", "SUBSYNC_EXTRA_SOURCES": "",
                           "SUBSYNC_LANGUAGES": ",".join(self.LANGS), "SUBSYNC_MAX_TRIES": "3"}, **env))
        return Worker(s, self.store, FakeAios(), None)

    def fakes(self, measures=None, passes=(), track=True):
        """check_original: never in sync (None with no track), measured as `measures` says, else 0.3; shift_sync: no
        steady offset; sync_subtitle: passes the ids in `passes`, else keeps the original. Each check and fit is logged
        in self.events, and self.on_check(id, the language's checks so far) is called after each check."""
        def cid(text, lang):
            return self.ids[(lang, text.split("\n")[1])]

        def check_original(index, text, lang, **kw):
            c = cid(text, lang)
            with self.lock:
                self.events.append(("check", c))
                n = sum(1 for kind, x in self.events if kind == "check" and x.startswith(lang))
            self.on_check(c, n)
            if not track:
                return None
            return {"in_sync": False, "route": "cues",
                    "gate_original": {"within200": (measures or {}).get(c, 0.3), "median_ms": 400}}

        def sync_subtitle(index, text, lang, **kw):
            c = cid(text, lang)
            with self.lock:
                self.events.append(("alass", c))
            if not track:
                return {"outcome": "keep_original", "reason": "no text track and the audio route is off", "route": None,
                        "waits_audio": True}
            if c in passes:
                return {"outcome": "synced", "reason": "passes the main rule", "route": "cues", "body": text,
                        "gate_synced": {"within200": 0.9, "median_ms": 30}}
            return {"outcome": "keep_original", "reason": "below the bar", "route": "cues",
                    "gate_original": {"within200": (measures or {}).get(c, 0.3), "median_ms": 400}}
        for name, fake in (("check_original", check_original), ("shift_sync", lambda *a, **k: None),
                           ("sync_subtitle", sync_subtitle)):
            patcher = mock.patch.object(pipeline, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def picked(self, w, *langs):
        """The file's job, queued; a pick for each of `langs` (the pick route's call)."""
        job = Job("movie", "tt1", {"filename": "Movie.mkv", "videoSize": 5})
        self.assertIs(w.submit(job), job)
        for lang in langs:
            w.prioritize(job.alias, lang)
        return job

    def judge(self, w, job, langs=None):
        """The file's text pass: each language judged (at once with SUBSYNC_PARALLEL_LANGUAGES)."""
        per_lang = {lang: [dict(c) for c in self.cands[lang]] for lang in (langs or self.LANGS)}
        w._sync_all(job, per_lang, lambda lang, cands: w._sync_language(job, FakeIndex(), "f-1", lang, cands, "abcd",
                                                                         "Movie.mkv"))

    def test_a_pick_doesnt_hold_back_the_other_languages_downloads(self):
        # Holding the other languages until the picked one's first round ended delayed their first copies
        # when a player asks for several languages one after another (Sherlock Russian and Spanish, 3 Idiots Russian),
        # and didn't make the picked language's own first copy sooner; every language's downloads go at once, as in
        # 1.4.0. The picked language's downloads hang until every other download started (at most 3 s: past that, the
        # others were held back)
        self.fakes()
        w = self.worker()                                        # SUBSYNC_PICK_WAIT_MS at its default, 14 s
        release = threading.Event()
        self.addCleanup(release.set)
        timer = threading.Timer(3.0, release.set)
        self.addCleanup(timer.cancel)
        for c in self.cands["spa"]:
            self.downloads.hold[c["url"]] = release
        others = {c["url"] for lang in ("eng", "rus") for c in self.cands[lang]}
        started = set()

        def on_start(url):
            if url in others:
                started.add(url)
                if started >= others:
                    release.set()
        self.downloads.on_start = on_start
        job = self.picked(w, "spa")
        timer.start()
        self.judge(w, job)
        log = self.downloads.log
        spa_ended = min(i for i, (what, url) in enumerate(log) if what == "end" and "/spa-" in url)
        self.assertEqual(len([url for what, url in log if what == "start" and url in others]), 16)
        self.assertLess(max(i for i, (what, url) in enumerate(log) if what == "start" and url in others), spa_ended)

    def test_one_language_at_a_time_keeps_the_languages_order_after_a_pick(self):
        self.fakes()
        w = self.worker(SUBSYNC_PARALLEL_LANGUAGES="off")
        self.judge(w, self.picked(w, "rus"))
        starts = [url for what, url in self.downloads.log if what == "start"]
        self.assertEqual([u.rsplit("/", 1)[1][:3] for u in starts], ["eng"] * 8 + ["rus"] * 8 + ["spa"] * 8)

    def held_in_try_order(self, lang):
        """The first download in try order is held until three others are checked, the fifth on until the fourth
        check: the first four checks are the 2nd to 4th in try order, then the 1st."""
        first, rest = threading.Event(), threading.Event()
        for event in (first, rest):
            self.addCleanup(event.set)
        self.downloads.hold[self.cands[lang][0]["url"]] = first
        for c in self.cands[lang][4:]:
            self.downloads.hold[c["url"]] = rest

        def on_check(cid, n):
            if n == 3:
                first.set()
            if n == 4:
                rest.set()
        self.on_check = on_check

    def logged_publish(self, w):
        real = w._publish

        def publish(alias, lang, key, final=False):
            with self.lock:
                self.events.append(("publish", self.store.get(key)["candidate_id"] if key else None))
            real(alias, lang, key, final)
        w._publish = publish

    def test_alass_runs_at_once_on_the_first_in_try_order_when_none_of_four_checked_fits(self):
        # Sherlock, 3 Idiots: alass ran only once every download was checked, after the pick's 14 s. On the first in try
        # order, not the best measured (eng-2 here): an "as it is" measure can't see a subtitle that is only
        # offset; published at once when it passes, then judging goes on as before
        self.fakes(measures={"eng-2": 0.7}, passes={"eng-0"})
        w = self.worker(SUBSYNC_LANGUAGES="eng")
        self.held_in_try_order("eng")
        job = self.picked(w, "eng")
        self.logged_publish(w)
        self.judge(w, job, ["eng"])
        self.assertEqual([kind for kind, _ in self.events[:4]], ["check"] * 4)
        self.assertEqual(self.events[3:6], [("check", "eng-0"), ("alass", "eng-0"), ("publish", "eng-0")])
        self.assertEqual(len([e for e in self.events if e[0] == "check"]), 8)
        # fitted once; alass on SUBSYNC_MAX_TRIES (3) in all, the early one among them
        self.assertEqual([c for kind, c in self.events if kind == "alass"], ["eng-0", "eng-1", "eng-2"])
        self.assertEqual(w.published(job.alias, "eng"), self.store.offers("f-1", ["eng"])[0]["key"])

    def test_a_language_not_picked_is_fitted_once_every_download_is_checked_as_before(self):
        self.fakes(measures={"eng-2": 0.7}, passes={"eng-0"})
        w = self.worker(SUBSYNC_LANGUAGES="eng")
        self.held_in_try_order("eng")
        job = self.picked(w)                                       # a download with no language
        self.judge(w, job, ["eng"])
        self.assertEqual([kind for kind, _ in self.events], ["check"] * 8 + ["alass"] * 3)
        self.assertEqual([c for kind, c in self.events if kind == "alass"], ["eng-0", "eng-1", "eng-2"])

    def test_another_language_of_the_picked_file_is_fitted_as_before(self):
        self.fakes()
        w = self.worker(SUBSYNC_LANGUAGES="eng,rus")
        self.held_in_try_order("rus")
        self.judge(w, self.picked(w, "eng"), ["eng", "rus"])
        rus = [kind for kind, c in self.events if c.startswith("rus")]
        self.assertEqual(rus, ["check"] * 8 + ["alass"] * 3)
        eng = [kind for kind, c in self.events if c.startswith("eng")]
        self.assertEqual(eng[4], "alass")
        self.assertEqual(eng.count("alass"), 3)

    def test_each_checked_original_is_measured_for_the_picks_answer_at_the_deadline(self):
        # the pick's answer when nothing passed: the original measured best as it is, when it beats the name-ranked
        # guess's measure or the guess wasn't measured
        self.fakes(measures={"eng-0": 0.4, "eng-5": 0.8, "rus-0": 0.9})
        w = self.worker()
        job = self.picked(w, "eng")
        w._set_originals(job.alias, [c for cs in self.cands.values() for c in cs], "Movie.mkv", None)
        self.assertEqual({lang: o["id"] for lang, o in w.known_originals(job.alias).items()},
                         {"eng": "eng-0", "rus": "rus-0", "spa": "spa-0"})          # the name-ranked guesses
        self.assertIsNone(w.measured_original(job.alias, "eng"))                     # nothing measured yet
        self.judge(w, job)
        self.assertEqual(w.measured_original(job.alias, "eng"), "eng-5")             # 80 % beats the guess's 40 %
        self.assertIsNone(w.measured_original(job.alias, "rus"))                     # the guess measured best
        self.assertIsNone(w.measured_original(job.alias, "spa"))                     # every one 30 %: the guess
        with w.cv:
            w.originals[job.alias]["spa"] = {"url": "http://subs.test/gone.srt", "id": "spa-gone"}
        self.assertIn(w.measured_original(job.alias, "spa"), {c["id"] for c in self.cands["spa"]})   # not measured

    def test_a_file_with_no_track_of_its_own_is_unchanged(self):
        # nothing can be checked against the file: no early alass and nothing measured (its anchored guesses stay)
        self.fakes(track=False)
        w = self.worker(SUBSYNC_LANGUAGES="eng")
        job = self.picked(w, "eng")
        self.judge(w, job, ["eng"])
        self.assertEqual([kind for kind, _ in self.events], ["check"] * 8 + ["alass"])   # one fit finds no track
        self.assertIsNone(w.measured_original(job.alias, "eng"))
        audio = Job("movie", "tt2", {"filename": "b.mp4", "videoSize": 5})
        audio.audio = True                                       # picked: the audio route of a file with no track
        w.submit(audio)
        w.prioritize(audio.alias, "eng")
        self.assertEqual(audio.picked, {})


    def test_the_no_track_case_is_told_by_the_pipelines_flag_not_its_words(self):
        # Only the audio route could judge it (waits_audio): one fit, then nothing more is fitted or stored,
        # whatever the reason says
        self.fakes(track=False)
        fake = pipeline.sync_subtitle
        with mock.patch.object(pipeline, "sync_subtitle",
                               lambda *a, **kw: dict(fake(*a, **kw), reason="the file has no subtitle track of its own")):
            w = self.worker(SUBSYNC_LANGUAGES="eng")
            self.judge(w, self.picked(w, "eng"), ["eng"])
        self.assertEqual([kind for kind, _ in self.events], ["check"] * 8 + ["alass"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)

if __name__ == "__main__":
    unittest.main()
