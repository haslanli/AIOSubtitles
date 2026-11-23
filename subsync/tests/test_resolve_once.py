"""A file's link (AIOStreams' playback address, which redirects to the debrid service's download address) is
followed once per reader; later reads go to where it pointed, and when that address stops answering, the link is asked
once more. Against a local server: no network."""
import logging
import threading
import unittest

from helpers import DebridLink, FIXTURES
from subsync.reader import MAX_RE_RESOLVE, RangeReader, ReadFailed, Refused


class ResolveOnceTest(unittest.TestCase):
    def setUp(self):
        self.d = DebridLink()

    def tearDown(self):
        self.d.close()

    def test_the_link_is_followed_once_then_reads_go_where_it_pointed(self):
        r = RangeReader(self.d.link, "test")
        self.assertEqual(r.get(0, 100), self.d.data[:100])
        self.assertEqual(r.get(5000, 300), self.d.data[5000:5300])
        self.assertEqual(b"".join(r.stream(9000, 4000, chunk=1000)), self.d.data[9000:13000])
        for k in range(20):                                 # the word reads: twenty small reads
            r.get(100 * k, 50)
        self.assertEqual(self.d.link_hits, 1)
        self.assertEqual(self.d.file_hits, 23)
        self.assertEqual(r.requests, 23)

    def test_an_address_that_stops_answering_is_looked_up_once_more(self):
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.expire()
        self.assertEqual(r.get(200, 100), self.d.data[200:300])
        self.assertEqual(self.d.link_hits, 2)
        self.assertEqual(r.get(400, 100), self.d.data[400:500])    # the new address is kept
        self.assertEqual(self.d.link_hits, 2)

    def test_a_stream_looks_the_link_up_again_too(self):
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.expire()
        self.assertEqual(b"".join(r.stream(1000, 2000, chunk=500)), self.d.data[1000:3000])
        self.assertEqual(self.d.link_hits, 2)

    def test_a_link_that_refuses_is_refused_once(self):
        self.d.link_status = 429
        with self.assertRaises(Refused):
            RangeReader(self.d.link, "test").get(0, 100)
        self.assertEqual(self.d.link_hits, 1)

    def test_a_refusal_after_an_expiry_is_still_a_refusal(self):
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.expire()
        self.d.link_status = 403
        with self.assertRaises(Refused):
            r.get(200, 100)
        self.assertEqual(self.d.link_hits, 2)              # the address once, the link once: no loop

    def test_a_429_at_the_address_is_a_refusal_not_a_new_look_up(self):
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.file_status = 429
        with self.assertRaises(Refused):
            r.get(200, 100)
        self.assertEqual(self.d.link_hits, 1)              # the worker backs off; the link is not asked again

    def test_an_address_that_keeps_failing_becomes_a_refusal(self):
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.file_status = 403                            # every address the link hands out answers 403
        with self.assertRaises(Refused):
            for k in range(10):
                r.get(100 * k, 50)
        self.assertLessEqual(self.d.link_hits, 1 + MAX_RE_RESOLVE)    # at most MAX_RE_RESOLVE fresh look-ups per reader

    def test_a_dead_address_asks_the_link_a_few_times_in_all(self):
        # The download address stops answering (no reply at all); the link still redirects to it, so
        # every look-up fails too. The reader asks the link at most 1 + MAX_RE_RESOLVE times, then no more
        self.assertEqual(MAX_RE_RESOLVE, 3)                 # the first look-up and at most 3 more
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.file_dead = True
        for k in range(20):
            with self.assertRaises(ReadFailed) as caught:
                r.get(100 * k, 50)
            self.assertNotIsInstance(caught.exception, Refused)
        self.assertLessEqual(self.d.link_hits, 1 + MAX_RE_RESOLVE)
        hits, counted = (self.d.link_hits, self.d.file_hits), r.requests
        with self.assertRaises(ReadFailed):
            list(r.stream(0, 1000))
        with self.assertRaises(ReadFailed):
            r.get(0, 1000)
        self.assertEqual((self.d.link_hits, self.d.file_hits), hits)     # used up: no request at all,
        self.assertEqual(r.requests, counted)                            # and none counted

    def test_the_link_answers_again_after_a_short_fault(self):
        r = RangeReader(self.d.link, "test")
        r.get(0, 100)
        self.d.file_dead = True
        with self.assertRaises(ReadFailed):
            r.get(100, 50)
        self.d.file_dead = False
        self.d.expire()
        self.assertEqual(r.get(200, 100), self.d.data[200:300])

    def test_a_link_without_a_redirect_is_never_capped(self):
        from helpers import RangeServer
        srv = RangeServer(FIXTURES)
        try:
            r = RangeReader(srv.url("tiny.mkv"), "test")
            for k in range(3 * (1 + MAX_RE_RESOLVE)):
                r.get(100 * k, 50)
            self.assertEqual(len(srv.headers), 3 * (1 + MAX_RE_RESOLVE))
        finally:
            srv.close()

    def test_reads_started_together_look_the_link_up_once(self):
        self.d.link_delay = 0.2
        r = RangeReader(self.d.link, "test")
        out, errors = {}, []

        def read(k):
            try:
                out[k] = r.get(1000 * k, 100)
            except Exception as e:                          # pragma: no cover: reported below
                errors.append(e)
        # daemon threads: one stuck on the reader's resolve lock fails the test, never holds up the run's exit
        threads = [threading.Thread(target=read, args=(k,), daemon=True) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual([t.is_alive() for t in threads], [False] * 4)   # none still waits for the lock
        self.assertEqual(errors, [])
        self.assertEqual({k: v for k, v in out.items()}, {k: self.d.data[1000 * k:1000 * k + 100] for k in range(4)})
        self.assertEqual(self.d.link_hits, 1)
        self.assertEqual(self.d.file_hits, 4)               # the first read's redirect, then three at the address

    def test_the_address_is_never_logged(self):
        r = RangeReader(self.d.link, "label")
        with self.assertLogs("subsync.read", level="INFO") as logs:
            r.get(0, 100)
            r.get(100, 100)
            self.d.expire()
            r.get(200, 100)
        text = "\n".join(logs.output)
        self.assertNotIn("/file/", text)
        self.assertNotIn("/link/", text)
        self.assertNotIn("127.0.0.1", text)

    def test_a_link_without_a_redirect_is_read_as_before(self):
        from helpers import RangeServer
        srv = RangeServer(FIXTURES)
        try:
            r = RangeReader(srv.url("tiny.mkv"), "test")
            r.get(0, 100)
            r.get(100, 100)
            self.assertEqual(len(srv.headers), 2)
            self.assertIsNone(r.resolved)
        finally:
            srv.close()


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
