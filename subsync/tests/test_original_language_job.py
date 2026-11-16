"""The title's original language in the job: the lookup starts as the request comes in and never holds the job; the
answer, when it is there by the time the file's index is read, is set on the index before references are chosen; a
file judged before the answer came is judged again once it is known, and only once."""
import os
import threading
import time
import unittest
from concurrent.futures import Future
from unittest import mock

from helpers import FakeDownloads, RangeServer, TempDir
from subsync import origlang, pipeline
from subsync.addon import App
from subsync.settings import Settings
from subsync.store import Store
from subsync.worker import Job, Safety, Worker
from test_io import realistic, srt_of

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class FakeLookup:
    """Answers by title as told; records every start; a start can be left running for ever."""

    def __init__(self, answers=None):
        self.answers, self.started = dict(answers or {}), []

    def start(self, video_id, key=""):
        self.started.append((video_id, key))
        return Future()          # never done: nothing may wait for it

    def peek(self, video_id):
        return self.answers.get(origlang.title_of(video_id))


class RuleTest(unittest.TestCase):
    def test_the_rule_a_verdict_is_recorded_under(self):
        self.assertEqual(origlang.rule("abc", None), "abc")                          # unknown: as before
        self.assertEqual(origlang.rule("abc", {"tmdb": "none", "wikidata": []}), "abc")
        self.assertEqual(origlang.rule("abc", {"tmdb": "eng"}), "abc:eng")
        self.assertEqual(origlang.rule("abc", {"override": "kor"}), "abc:kor")
        self.assertEqual(origlang.rule("abc", {"wikidata": ["jpn", "eng", "fre"]}), "abc:jpn+eng+fre")

    def test_which_recorded_rule_still_counts(self):
        current = origlang.current
        self.assertTrue(current("abc")("abc"))
        self.assertTrue(current("abc")("abc:eng"))         # judged with a language, asked now under an id with none
        self.assertTrue(current("abc:eng")("abc:jpn"))     # judged with a language: kept, whatever id asks now
        self.assertTrue(current("abc:eng")("abc:eng"))
        self.assertFalse(current("abc:eng")("abc"))        # judged before the answer came: judged again, once
        self.assertFalse(current("abc")("old"))            # another pass rule
        self.assertFalse(current("abc:eng")("old:eng"))
        self.assertFalse(current("abc")(None))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.srv = RangeServer(FIXTURES)
        self.addCleanup(self.srv.close)
        self.size = os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))
        self.hints = {"filename": "tiny.mkv", "videoSize": self.size}
        url = "http://subs.test/e.srt"
        self.downloads = FakeDownloads({url: srt_of(realistic(60, 3))})
        srv = self.srv

        class Listing:
            def find_row(self, *a):
                return {"url": srv.url("tiny.mkv")}

            def subtitles(self, *a):
                return [{"id": "e", "lang": "eng", "url": url, "release": "tiny"}]
        self.listing = Listing()
        self.lookup = FakeLookup()

    def worker(self, **env):
        self.s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_EXTRA_SOURCES": "",
                                "SUBSYNC_PUBLIC_BASE": "http://subsync:8790"}, **env))
        self.store = Store(self.dir)
        return Worker(self.s, self.store, self.listing, Safety(), lookup=self.lookup)

    def run_job(self, w, video_id="tt1375666"):
        seen = []
        real = pipeline.choose_reference

        def spy(index, *a, **kw):
            seen.append(getattr(index, "original_language", None))
            return real(index, *a, **kw)
        job = Job("movie", video_id, self.hints)
        with self.downloads.patch(), mock.patch.object(pipeline, "choose_reference", spy):
            w.process(job)
        return job, seen

    def detail(self):
        import json
        (d,) = self.store.db.execute("SELECT detail FROM files").fetchone()
        return json.loads(d)


class JobTest(Base):
    def test_the_answer_is_on_the_index_before_references_are_chosen(self):
        self.lookup.answers["tt1375666"] = {"tmdb": "jpn", "wikidata": ["jpn"], "anime": None, "failed": []}
        w = self.worker(SUBSYNC_TMDB_KEY="0123456789abcdef0123456789abcdef")
        job, seen = self.run_job(w)
        self.assertEqual(self.lookup.started[0], ("tt1375666", "0123456789abcdef0123456789abcdef"))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"jpn"})
        d = self.detail()
        self.assertEqual(d["original_language"], {"lang": "jpn", "via": "tmdb", "tmdb": "jpn", "wikidata": ["jpn"]})
        self.assertEqual(d["rule"], self.s.rule_id + ":jpn")
        self.assertEqual(job.rule, self.s.rule_id + ":jpn")

    def test_without_an_answer_nothing_waits_and_the_rules_are_as_before(self):
        w = self.worker()
        t0 = time.monotonic()
        job, seen = self.run_job(w)
        self.assertLess(time.monotonic() - t0, 30)       # the never-finishing lookup held nothing
        self.assertEqual(len(self.lookup.started), 1)
        self.assertEqual(set(seen), {None})
        d = self.detail()
        self.assertNotIn("original_language", d)
        self.assertEqual(d["rule"], self.s.rule_id)

    def test_a_disagreement_is_recorded(self):
        self.lookup.answers["tt2788316"] = {"tmdb": "eng", "wikidata": ["jpn"], "anime": None, "failed": []}
        self.run_job(self.worker(), "tt2788316:1:1")
        self.assertEqual(self.detail()["original_language"],
                         {"lang": "eng", "via": "tmdb", "tmdb": "eng", "wikidata": ["jpn"], "disagree": True})

    def test_the_persons_key_goes_with_the_lookup(self):
        w = self.worker(SUBSYNC_TMDB_KEY="eyJ.persons-own-token")
        self.run_job(w)
        self.assertEqual(self.lookup.started[0][1], "eyJ.persons-own-token")

    def test_no_lookup_no_change(self):
        self.lookup = None
        w = self.worker()
        job, seen = self.run_job(w)
        self.assertEqual(set(seen), {None})
        self.assertEqual(self.detail()["rule"], self.s.rule_id)


class JudgedAgainTest(Base):
    def test_a_file_judged_without_the_answer_is_judged_again_once_it_is_known(self):
        w = self.worker()
        bt = "bt:" + "a" * 40 + "|0"
        row = {"url": self.srv.url("tiny.mkv")}

        def once():
            before = self.store.bytes_today("read")
            with self.downloads.patch():
                w._process_row(Job("movie", "tt1375666", self.hints), self.hints, row, bt)
            return self.store.bytes_today("read") - before
        self.assertGreater(once(), 0)                    # first read; the answer isn't there yet
        self.assertEqual(once(), 0)                      # judged lately under the same rule: nothing read
        self.lookup.answers["tt1375666"] = {"tmdb": "eng", "wikidata": ["eng"], "anime": None, "failed": []}
        self.assertGreater(once(), 0)                    # the answer came: judged again (the index read again)
        self.assertEqual(once(), 0)                      # and only once
        self.assertEqual(len(self.lookup.started), 4)    # the lookup was started on every request, shortcut or not

    def test_a_file_asked_for_under_another_kind_of_id_isnt_read_again(self):
        # the same file as tt1375666 (its language known) and as tmdb:27205 (no lookup for that kind of id)
        self.lookup.answers["tt1375666"] = {"tmdb": "eng", "wikidata": ["eng"], "anime": None, "failed": []}
        w = self.worker()
        bt = "bt:" + "b" * 40 + "|0"
        row = {"url": self.srv.url("tiny.mkv")}

        def once(video_id):
            before = self.store.bytes_today("read")
            with self.downloads.patch():
                w._process_row(Job("movie", video_id, self.hints), self.hints, row, bt)
            return self.store.bytes_today("read") - before
        self.assertGreater(once("tt1375666"), 0)
        for video_id in ("tmdb:27205", "tt1375666", "tmdb:27205"):
            self.assertEqual(once(video_id), 0, video_id)
        app = App(self.s, self.store, w, Safety())
        self.store.alias("name:c.mkv|1000", "f-2")
        self.store.file_read("f-2", 1000, "f-2", True, {"rule": self.s.rule_id + ":eng"})
        app.subtitles("movie", "tmdb:27205", "filename=c.mkv&videoSize=1000")
        self.assertEqual(len(w.jobs), 0)

    def test_a_stored_verdict_under_another_language_is_judged_again(self):
        w = self.worker()
        key = self.store.put("f-1", "a" * 64, "eng", 0, "keep_original", "below the bar",
                             detail={"rule": self.s.rule_id})
        self.assertIsNotNone(w.fresh(key, self.s.rule_id))
        self.assertIsNone(w.fresh(key, self.s.rule_id + ":eng"))         # judged before the language was known
        known = self.store.put("f-1", "b" * 64, "eng", 1, "keep_original", "below the bar",
                               detail={"rule": self.s.rule_id + ":jpn"})
        self.assertIsNotNone(w.fresh(known, self.s.rule_id))                # asked under an id with no answer
        self.assertIsNotNone(w.fresh(known, self.s.rule_id + ":eng"))
        self.assertIsNotNone(w.fresh(key))                                     # no job's rule: the service's

    def test_the_addon_reads_a_file_again_once_its_answer_is_known(self):
        w = self.worker()
        app = App(self.s, self.store, w, Safety())
        self.store.alias("name:a.mkv|1000", "f-1")
        self.store.file_read("f-1", 1000, "f-1", True, {"rule": self.s.rule_id})
        app.subtitles("movie", "tt1375666", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(w.jobs), 0)                                       # read lately under this rule
        self.assertEqual(self.lookup.started[-1][0], "tt1375666")             # the lookup started all the same
        self.lookup.answers["tt1375666"] = {"tmdb": "eng", "wikidata": [], "anime": None, "failed": []}
        app.subtitles("movie", "tt1375666", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(w.jobs), 1)                                       # the answer came: read again now
        w.jobs.clear()
        w.queue.clear()
        self.store.file_read("f-1", 1000, "f-1", True, {"rule": self.s.rule_id + ":eng"})
        app.subtitles("movie", "tt1375666", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(w.jobs), 0)


if __name__ == "__main__":
    unittest.main()


class RealLookupTest(Base):
    """The real lookup inside a worker (stand-in TMDB and Wikidata): the second request of a file finds the answer."""

    def test_the_answer_reaches_the_file(self):
        from test_origlang import Services
        srv = Services()
        self.addCleanup(srv.close)
        srv.find["tt1375666"] = {"movie_results": [{"id": 27205, "original_language": "ja"}]}
        env = {"SUBSYNC_TMDB_URL": srv.base + "/tmdb", "SUBSYNC_WIKIDATA_URL": srv.base + "/sparql",
               "SUBSYNC_TMDB_KEY": "0123456789abcdef0123456789abcdef"}
        w = self.worker(**env)
        w.lookup = origlang.make_lookup(self.s, self.store)
        self.addCleanup(w.lookup.close)
        bt = "bt:" + "c" * 40 + "|0"
        row = {"url": self.srv.url("tiny.mkv")}
        for _ in range(2):
            with self.downloads.patch():
                w._process_row(Job("movie", "tt1375666", self.hints), self.hints, row, bt)
            deadline = time.monotonic() + 10
            while w.lookup.peek("tt1375666") is None and time.monotonic() < deadline:
                time.sleep(0.05)
        d = self.detail()
        self.assertEqual(d["original_language"]["lang"], "jpn")
        self.assertEqual(d["rule"], self.s.rule_id + ":jpn")
