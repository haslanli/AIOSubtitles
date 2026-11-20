"""Store, safety, sources filters and the addon's answers."""
import errno
import gzip
import io
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import tracemalloc
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import (answers, Clock, closed_after, dialogue_srt, exchange, FakeDownloads, local_network_only,  # noqa: E402
                     RangeServer, start_server, stop_server, TempDir, watch_the_log)
from subsync import ALGO_VERSION  # noqa: E402
from subsync.addon import REPROCESS_S, App, file_id, serve, token  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync import explain, sources  # noqa: E402
from subsync.sources import (Aios, expected_size, extras, fetch_subtitle, parse_extras, same_hash,  # noqa: E402
                             unreadable)
from subsync.store import REFUSED_BACKOFF_S, Store, result_key  # noqa: E402
from subsync.reader import Stopped  # noqa: E402
from subsync.worker import Job, JobGuard, Safety, Worker, rewrite  # noqa: E402

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
FILE = "0123456789abcdef-1000"
# the originals the routes download and check before serving them: short, so the check gives no verdict
X_TEXT = "1\n00:00:01,000 --> 00:00:02,000\nthe original\n"
ORIGINALS = {"http://opensubs/x.srt": X_TEXT, "http://opensubs/y.srt": "1\n00:00:01,000 --> 00:00:02,000\nanother\n",
             "http://opensubs/r.srt": "1\n00:00:01,000 --> 00:00:02,000\nоригинал\n"}
SERVED = "X-AIOSubtitles-Served"   # what a pick, pending or "(original)" answer is


def fake_downloads(test):
    """Subtitle downloads for a test: ORIGINALS, nothing from the network."""
    downloads = FakeDownloads(ORIGINALS)
    for patcher in (downloads.patch(), local_network_only()):
        patcher.start()
        test.addCleanup(patcher.stop)
    return downloads


class StoreTest(unittest.TestCase):
    def test_synced_is_offered_and_rank_rules(self):
        with TempDir() as d:
            st = closed_after(self, Store(d))
            k1 = st.put(FILE, SHA_A, "eng", 1, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n", fmt="srt")
            self.assertEqual([r["key"] for r in st.offers(FILE, ["eng", "rus"])], [k1])
            st.put(FILE, SHA_B, "eng", 2, "synced", body="b", fmt="srt")      # worse rank: keeps the last good
            self.assertEqual(st.last_good(FILE, "eng")["key"], k1)
            k3 = st.put(FILE, SHA_C, "eng", 0, "synced", body="c", fmt="srt")  # better rank replaces it
            self.assertEqual(st.last_good(FILE, "eng")["key"], k3)
            st.put(FILE, SHA_A[::-1].replace("a", "d"), "eng", 0, "keep_original")  # a failing new one: last good stays
            self.assertEqual(st.last_good(FILE, "eng")["key"], k3)
            st.put(FILE, "e" * 64, "eng", 0, "in_sync")                       # first subtitle fine as it is: nothing offered
            self.assertEqual(st.offers(FILE, ["eng"]), [])
            self.assertEqual(st.body(k3), "c")

    def test_a_title_is_never_kept_as_a_files_name(self):
        # An earlier version linked the file read for a request with a hash but no size to the title alone
        # ("title:<type>/<id>"), so every other release of the title got its copies; those links go when the store
        # opens, and the file's results and its own names stay
        with TempDir() as d:
            st = Store(d)
            key = st.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n", fmt="srt")
            for alias in ("title:movie/tt1", "title:series/tt2:1:1", "hash:ab|3", "name:a.mkv|3"):
                st.alias(alias, FILE)
            st.close()
            st = closed_after(self, Store(d))
            self.assertEqual([st.resolve(a) for a in ("title:movie/tt1", "title:series/tt2:1:1", "hash:ab|3",
                                                      "name:a.mkv|3")], [None, None, FILE, FILE])
            self.assertEqual([r["key"] for r in st.offers(FILE, ["eng"])], [key])

    def test_cap_evicts_least_recently_served(self):
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, cap_bytes=10 ** 9, clock=clock))
            body = os.urandom(200_000).hex()   # incompressible-ish
            keys = []
            for i, sha in enumerate((SHA_A, SHA_B, SHA_C)):
                clock.t += 10
                keys.append(st.put(f"{i:016x}-1", sha, "eng", 0, "synced", body=body, fmt="srt"))
            clock.t += 10
            st.served(keys[0])
            st.cap = st.copy_bytes() - 1       # one body must go: the least recently served is keys[1]
            st.enforce_cap()
            self.assertIsNotNone(st.get(keys[0]))
            self.assertIsNone(st.get(keys[1]))
            self.assertIsNotNone(st.get(keys[2]))

    def test_the_cap_counts_the_copies_only(self):
        # It counted the database too, which deleting copies never shrinks: once the database alone was near
        # the cap every copy went, and each new one right after it was made; now that is logged once
        from subsync.settings import MB
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, cap_bytes=32 * 1024, clock=clock))
            text = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
            keys = []
            with self.assertLogs("subsync.store", "WARNING") as logs:
                for i in range(3):
                    clock.t += 10
                    keys.append(st.put(f"{i:016x}-1", SHA_A, "eng", 0, "synced", body=text, fmt="srt"))
            self.assertGreater(st.database_bytes(), st.cap)                # the database alone is past the cap
            self.assertEqual([k for k in keys if st.get(k)], keys)         # and no copy went for it
            self.assertEqual(logs.output, ["WARNING:subsync.store:the store's database alone is larger than "
                                           "SUBSYNC_STORE_CAP_MB (0.03125 MB); copies aren't deleted for it"])
            self.assertEqual(st.cap / MB, 0.03125)
            stats = st.stats()
            self.assertEqual((stats["bytes"], stats["database_bytes"]), (st.copy_bytes(), st.database_bytes()))
            self.assertEqual(stats["bytes"], sum(st.get(k)["body_bytes"] for k in keys))
            st.cap = stats["bytes"] - 1                                    # the copies past it: the one served
            clock.t += 10                                                  # longest ago goes
            st.served(keys[0])
            st.enforce_cap()
            self.assertEqual([k for k in keys if st.get(k)], [keys[0], keys[2]])

    def test_lifetimes_and_read_failure_backoff(self):
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, clock=clock))
            k = st.put(FILE, SHA_A, "eng", 0, "keep_original")
            self.assertIsNotNone(st.fresh(k))
            clock.t += 31 * 86400
            self.assertIsNone(st.fresh(k))                 # retried after 30 days
            waits = []
            for _ in range(5):
                r = st.file_failed("name:x|1")
                waits.append(None if r is None else round(r - clock.t))
            self.assertEqual(waits, [600, 3600, 21600, 86400, None])          # 10 minutes first
            self.assertTrue(st.file_blocked("name:x|1"))
            waits = []
            for _ in range(4):                       # a refused file, or one neither MKV nor MP4: an hour first, as before
                r = st.file_failed("name:y|1", REFUSED_BACKOFF_S)
                waits.append(None if r is None else round(r - clock.t))
            self.assertEqual(waits, [3600, 21600, 86400, None])
            ks = st.put(FILE, SHA_B, "eng", 0, "synced", body="x", fmt="srt")
            clock.t += 181 * 86400
            st.expire()
            self.assertIsNone(st.get(ks))                  # 180 days after last served

    def test_a_files_names_details_and_marks_go_180_days_after_it_was_last_asked_for(self):
        # Aliases, files and over_cap rows were never deleted, so a file's names outlived its timings for
        # good; a mark a request still meets stays, so such a file is still never read again
        DAY = 86400
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, clock=clock))

            def rows():
                return {table: sorted(r[0] for r in st.db.execute(f"SELECT {column} FROM {table}"))
                        for table, column in (("aliases", "alias"), ("files", "file_key"), ("over_cap", "alias"))}
            served, unused, again = "1" * 16 + "-5", "2" * 16 + "-5", "3" * 16 + "-5"
            for key in (served, unused, again):          # three files read, each known by two names
                for alias in (f"name:{key[:1]}.mkv|5", f"hash:{key[:16]}|5"):
                    st.alias(alias, key)
                st.file_read(key, 5, key[:16], True, {"tracks": []})
            copy = st.put(served, SHA_A, "eng", 0, "synced", body="x", fmt="srt")
            for alias in ("name:broken.mkv|5", "name:asked.mkv|5"):    # read failures: the second never read again
                for _ in range(5 if alias == "name:asked.mkv|5" else 1):
                    st.file_failed(alias)
            st.cap_reached("name:huge.mkv|5", 1000)
            st.cap_reached("name:asked-huge.mkv|5", 1000)
            clock.t += 100 * DAY
            st.served(copy)                                          # its copy is still served
            st.alias(f"name:{again[:1]}.mkv|5", again)              # asked for again under one of its names
            st.marks_asked("name:asked.mkv|5", "name:asked-huge.mkv|5", None)   # requests met these marks
            clock.t += 100 * DAY                                     # 200 days after the reads
            st.expire()
            self.assertEqual(rows(), {"aliases": sorted(["name:1.mkv|5", "hash:1111111111111111|5", "name:3.mkv|5",
                                                         "hash:3333333333333333|5"]),
                                      "files": sorted([served, again, "name:asked.mkv|5"]),
                                      "over_cap": ["name:asked-huge.mkv|5"]})
            self.assertTrue(st.file_blocked("name:asked.mkv|5"))
            self.assertEqual(st.over_cap("name:asked-huge.mkv|5"), 1000)
            self.assertEqual(st.resolve("name:1.mkv|5"), served)
            clock.t += 181 * DAY                                     # nobody asked since, the copy not served since
            st.expire()
            self.assertEqual(rows(), {"aliases": [], "files": [], "over_cap": []})

    def test_budget_per_day_and_kind(self):
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, clock=clock))
            st.day_add("read", 100)
            st.day_add("read", 50)
            st.day_add("other", 7)
            self.assertEqual(st.day_count("read"), 150)
            clock.t += 86400
            self.assertEqual(st.day_count("read"), 0)

    def test_key_format(self):
        # The language is part of the key, so each language keeps its own verdict
        self.assertEqual(result_key(FILE, "eng", SHA_A), f"subsync:{ALGO_VERSION}:{FILE}:eng:{'a' * 16}")

    def test_one_subtitle_under_two_languages_keeps_a_verdict_for_each(self):
        # The same bytes listed as English and (mislabelled) Spanish shared one row: Spanish's language check
        # wrote last and English's synced copy was no longer offered, nor judged again for 30 days
        for keep_texts in (True, False):
            with self.subTest(keep_texts=keep_texts), TempDir() as d, Store(d, keep_texts=keep_texts) as st:
                text = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
                eng = st.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt", original=text)
                spa = st.put(FILE, SHA_A, "spa", 0, "keep_original", "not Spanish text")
                self.assertNotEqual(eng, spa)
                self.assertEqual([r["key"] for r in st.offers(FILE, ["eng", "spa"])], [eng])
                self.assertEqual((st.fresh(eng)["outcome"], st.fresh(eng)["lang"]), ("synced", "eng"))
                self.assertEqual((st.fresh(spa)["outcome"], st.fresh(spa)["lang"]), ("keep_original", "spa"))

    def test_results_stored_before_are_keyed_by_language_once_with_nothing_judged_again(self):
        # Rows keyed without their language get it at the next start, with their copies, offers and last_good
        text = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
        synced, kept = result_key(FILE, "eng", SHA_A), result_key(FILE, "rus", SHA_B)
        before = {synced: f"subsync:{ALGO_VERSION}:{FILE}:{'a' * 16}", kept: f"subsync:{ALGO_VERSION}:{FILE}:{'b' * 16}"}
        for keep_texts in (True, False):
            with self.subTest(keep_texts=keep_texts), TempDir() as d:
                with Store(d, keep_texts=keep_texts) as st:
                    st.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt", original=text)
                    st.put(FILE, SHA_B, "rus", 1, "keep_original", "nothing passes")
                    st.set_offers(FILE, "eng", [synced])
                    for key, old in before.items():      # as the earlier version keyed them, and named their copies
                        for path, moved in ((st._body_path(key), st._body_path(old)),
                                            (st._recipe_path(key), st._recipe_path(old))):
                            if os.path.exists(path):
                                os.replace(path, moved)
                        for table in ("results", "last_good", "offers"):
                            st.db.execute(f"UPDATE {table} SET key=? WHERE key=?", (old, key))
                    copy = st.recipe(before[synced]) if not keep_texts else st.body(before[synced])
                    self.assertIsNotNone(copy)
                for _ in range(2):                       # once: a second start finds nothing to change
                    with Store(d, keep_texts=keep_texts) as st:
                        self.assertEqual([r["key"] for r in st.offers(FILE, ["eng"])], [synced])
                        self.assertEqual(st.last_good(FILE, "eng")["key"], synced)
                        self.assertEqual(st.fresh(synced)["outcome"], "synced")      # nothing to judge again
                        self.assertEqual(st.fresh(kept)["reason"], "nothing passes")
                        self.assertEqual(st.recipe(synced) if not keep_texts else st.body(synced), copy)
                        self.assertEqual(st.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 2)
                left = [n for _, _, names in os.walk(d) for n in names if n.endswith(".gz")]
                self.assertEqual(len(left), 1)          # the one copy, under its new name only

    def test_file_state_is_when_a_file_was_read_and_what_its_read_found(self):
        # What the addon asks of a file's read, through the store's lock and on its clock
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, clock=clock))
            self.assertEqual((st.file_state(FILE), st.file_detail(FILE)), ((None, None), None))
            st.file_read(FILE, 1000, FILE[:16], True, {"reference": False})
            clock.t += 10
            self.assertEqual(st.file_state(FILE), (clock.t - 10, {"reference": False}))
            self.assertEqual(st.file_detail(FILE), {"reference": False})
            st.file_failed("name:x|1")                     # a read that failed: its time, no detail
            self.assertEqual((st.file_state("name:x|1"), st.file_detail("name:x|1")), ((clock.t, {}), {}))

    def test_read_failures_count_in_a_row_each_back_off_its_own(self):
        # A read that succeeds clears the failures before it, and a refusal (or a file neither MKV nor MP4)
        # after read failures starts its own 1 h / 6 h / 24 h instead of stopping the file for good
        with TempDir() as d, Store(d, clock=Clock()) as st:
            waits = lambda calls: [None if r is None else round(r - st.clock()) for r in calls]   # noqa: E731
            self.assertEqual(waits([st.file_failed("name:x|1") for _ in range(3)]), [600, 3600, 21600])
            self.assertEqual(waits([st.file_failed("name:x|1", REFUSED_BACKOFF_S)]), [3600])
            self.assertEqual(st.file_retry("name:x|1"), st.clock() + 21600)   # the later of the two
            self.assertTrue(st.file_blocked("name:x|1"))
            st.file_ok("name:x|1")
            self.assertFalse(st.file_blocked("name:x|1"))
            self.assertIsNone(st.file_retry("name:x|1"))
            self.assertEqual(waits([st.file_failed("name:x|1"), st.file_failed("name:x|1", REFUSED_BACKOFF_S)]),
                             [600, 3600])
            st.file_ok("name:other|1")                     # another file's read clears nothing of this one
            self.assertTrue(st.file_blocked("name:x|1"))

    def test_a_damaged_copy_is_dropped_and_judged_again(self):
        # A copy file cut short, garbled or not gzip reads as no copy, and its row goes (it was offered
        # still, and every request for it failed); a file that is only missing keeps its row, and an OS error that
        # says nothing of the file (too many open files) is not taken for damage
        def damage(path, how):
            with open(path, "rb") as f:
                raw = f.read()
            mid = len(raw) // 2   # in the compressed data, past the header and the file name it holds
            raw = {"cut": raw[:mid], "garbled": raw[:mid] + bytes(b ^ 0xFF for b in raw[mid:mid + 40]) + raw[mid + 40:],
                   "not gzip": b"1\n00:00:01,000 --> 00:00:02,000\nx\n", "not json": gzip.compress(b"{not json")}[how]
            with open(path, "wb") as f:
                f.write(raw)

        text = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nline {i}\n\n" for i in range(1, 50))
        for keep_texts, how in ((True, "cut"), (True, "garbled"), (True, "not gzip"), (False, "cut"),
                                (False, "not json")):
            with self.subTest(keep_texts=keep_texts, damage=how), TempDir() as d, \
                    Store(d, keep_texts=keep_texts) as st:
                k = st.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt", original=text)
                self.assertEqual(len(st.offers(FILE, ["eng"])), 1)
                damage(st._copy_path(k), how)
                with self.assertLogs("subsync.store", "WARNING"):
                    self.assertIsNone(st.body(k) if keep_texts else st.recipe(k))
                self.assertIsNone(st.get(k))
                self.assertEqual(st.offers(FILE, ["eng"]), [])
                self.assertFalse(os.path.exists(st._copy_path(k)))
        with TempDir() as d, Store(d) as st:
            k = st.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt")
            with mock.patch("subsync.store.gzip.open", side_effect=OSError(errno.EMFILE, "Too many open files")):
                with self.assertRaises(OSError):
                    st.body(k)
            self.assertIsNotNone(st.get(k))
            os.remove(st._copy_path(k))
            self.assertIsNone(st.body(k))
            self.assertIsNotNone(st.get(k))

    def test_a_verdict_with_no_copy_removes_the_copy_an_earlier_verdict_made(self):
        # A synced copy judged again as keep_original (another pass rule, the language check) left its file
        # on disk for good, uncounted by the cap; so did a public copy judged again that isn't a re-timing any more
        text = "1\n00:00:01,000 --> 00:00:02,000\nthe line\n"
        for keep_texts, again in ((True, "keep_original"), (True, "in_sync"), (False, "keep_original"),
                                  (False, "synced")):
            with self.subTest(keep_texts=keep_texts, again=again), TempDir() as d, \
                    Store(d, keep_texts=keep_texts) as st:
                k = st.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt", original=text)
                self.assertTrue(os.path.exists(st._copy_path(k)))
                # public mode: a body that isn't a re-timing of its original keeps no recipe
                st.put(FILE, SHA_A, "eng", 0, again, body="something else" if again == "synced" else None,
                       original=text if again == "synced" else None)
                self.assertFalse(os.path.exists(st._copy_path(k)))
                self.assertEqual(st.get(k)["body_bytes"], 0)
                self.assertEqual(st.offers(FILE, ["eng"]), [])

    def test_a_copy_that_cannot_be_written_leaves_no_temporary_file(self):
        # A full disk while the copy is written left its partial temporary file for good, outside the cap
        real_open = gzip.open

        class Full:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def write(self, _):
                raise OSError(errno.ENOSPC, "No space left on device")

        def full_disk(path, mode="rb", **kw):
            f = real_open(path, mode, **kw)
            return Full(f) if "w" in mode else f

        with TempDir() as d, Store(d) as st:
            with mock.patch("subsync.store.gzip.open", side_effect=full_disk):
                with self.assertRaises(OSError):
                    st.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n", fmt="srt")
            left = [n for _, _, names in os.walk(d) for n in names if not n.startswith("subsync.sqlite3")]
            self.assertEqual(left, [])
            self.assertIsNone(st.get(result_key(FILE, "eng", SHA_A)))

    def test_two_puts_of_one_key_at_once_write_two_temporary_files(self):
        # Both wrote into the key's one ".tmp" file; the second to put it in place found it gone (or the two
        # writes mixed into one damaged copy)
        real_open, both = gzip.open, threading.Barrier(2, timeout=10)
        written, errors = [], []

        def meet(path, mode="rb", **kw):
            if "w" in mode:
                written.append(path)
                both.wait()   # the two puts are writing at the same time
            return real_open(path, mode, **kw)

        with TempDir() as d, Store(d) as st:
            texts = [f"1\n00:00:01,000 --> 00:00:02,000\n{word}\n" for word in ("first", "second")]

            def put(text):
                try:
                    st.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt")
                except Exception as e:
                    errors.append(e)
            with mock.patch("subsync.store.gzip.open", side_effect=meet):
                threads = [threading.Thread(target=put, args=(t,)) for t in texts]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(20)
            self.assertEqual(errors, [])
            self.assertEqual(len(set(written)), 2)
            self.assertIn(st.body(result_key(FILE, "eng", SHA_A)), texts)
            left = [n for _, _, names in os.walk(d) for n in names if n.endswith(".tmp")]
            self.assertEqual(left, [])

    def test_the_cap_is_checked_when_a_copy_is_added_and_evicts_in_one_pass(self):
        # Every put summed the whole table and listed the folder, a verdict with no copy too; over the cap,
        # each copy dropped summed it again
        with TempDir() as d:
            clock = Clock()
            with Store(d, cap_bytes=10 ** 9, clock=clock) as st:
                keys = []
                for i in range(10):
                    clock.t += 10
                    keys.append(st.put(f"{i:016x}-1", SHA_A, "eng", 0, "synced", body=os.urandom(100_000).hex(),
                                       fmt="srt"))
                clock.t += 10
                for k in keys[:3]:                       # served again: the least recently served are keys[3:]
                    st.served(k)
                with mock.patch.object(st, "copy_bytes", wraps=st.copy_bytes) as summed:
                    st.put(FILE, SHA_B, "eng", 0, "keep_original")
                    st.put(FILE, SHA_C, "eng", 0, "in_sync")
                    self.assertEqual(summed.call_count, 0)
                    st.put(FILE, "e" * 64, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n", fmt="srt")
                    self.assertEqual(summed.call_count, 1)
                    sizes = {k: st.get(k)["body_bytes"] for k in keys}
                    st.cap = st.copy_bytes() - sum(sizes[k] for k in keys[3:6]) - sizes[keys[6]] // 4
                    summed.reset_mock()
                    st.enforce_cap()                     # keys[3] to keys[6] go, in one pass
                    self.assertEqual(summed.call_count, 2)
                self.assertEqual([k for k in keys if st.get(k)], keys[:3] + keys[7:])
                self.assertLessEqual(st.copy_bytes(), st.cap)

    def test_eviction_and_drops_use_indexes_and_one_transaction(self):
        # A dropped copy's last_good and offers rows were found by scanning both tables, and expire, the
        # eviction, set_offers and judged_put committed (and synced) each statement on its own
        with TempDir() as d, Store(d, clock=Clock()) as st:
            def plan(sql, *args):
                return " ".join(row[-1] for row in st.db.execute("EXPLAIN QUERY PLAN " + sql, args))
            self.assertIn("results_lru", plan("SELECT key FROM results WHERE body_bytes>0 ORDER BY last_served"))
            self.assertIn("last_good_key", plan("DELETE FROM last_good WHERE key=?", "k"))
            self.assertIn("offers_key", plan("DELETE FROM offers WHERE key=?", "k"))
            keys = [st.put(f"{i:016x}-1", SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n",
                           fmt="srt") for i in range(3)]
            statements = []
            st.db.set_trace_callback(statements.append)
            st.set_offers(FILE, "eng", keys)
            st.judged_put(FILE, ["eng", "spa"], "r1")
            st.clock.t += 181 * 86400
            self.assertEqual(st.expire(), 3)
            st.db.set_trace_callback(None)
            self.assertEqual([s.split()[0] for s in statements if s.split()[0] in ("BEGIN", "COMMIT", "ROLLBACK")],
                             ["BEGIN", "COMMIT"] * 3)
            self.assertEqual(st.offers(FILE, ["eng"]), [])

    def test_the_store_commits_without_syncing_and_closes(self):
        # Every commit of the store (a listing's served copies, each range read's bytes) synced the disk;
        # the people's records keep doing so. The store couldn't be closed
        from subsync.users import Users
        with TempDir() as d:
            with Store(d) as st:
                self.assertEqual(st.db.execute("PRAGMA synchronous").fetchone()[0], 1)   # NORMAL
            with self.assertRaises(sqlite3.ProgrammingError):
                st.db.execute("SELECT 1")
            again = Store(d)
            again.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                again.day_count("read")
            users = Users(os.path.join(d, "users.sqlite3"), b"s" * 32)
            self.addCleanup(users.close)
            self.assertEqual(users.db.execute("PRAGMA synchronous").fetchone()[0], 2)   # FULL


class SafetyTest(unittest.TestCase):
    def test_guard_rules(self):
        with TempDir() as d:
            clock = Clock()
            st = closed_after(self, Store(d, clock=clock))
            safety = Safety(backoff_s=900, clock=clock)
            g = JobGuard(safety, st, daily_budget=1000)
            g.before_read(500)
            g.after_read(500)
            with self.assertRaises(Stopped):
                g.before_read(600)                         # the day's budget, counted as bytes received
            other = JobGuard(safety, st, daily_budget=10 ** 12)
            other.before_read(1)
            self.assertEqual(safety.refused(), 900)
            self.assertEqual(safety.refused(), 900)        # during its pause, the same refusal (not doubled)
            clock.t += 900                                 # the pause ended: the next refusal is one in a row
            self.assertEqual(safety.refused(), 1800)
            with self.assertRaises(Stopped):
                other.before_read(1)                       # a refusal stops every read
            clock.t += 1801
            other.before_read(1)
            other.after_read(1)
            self.assertEqual(safety.refused(), 900)        # a good read resets the doubling

    def test_rewrite(self):
        self.assertEqual(rewrite("https://jf.example/api/v1/proxy/x", "https://jf.example=http://aiostreams:3000"),
                         "http://aiostreams:3000/api/v1/proxy/x")
        self.assertEqual(rewrite("http://other/x", "https://jf.example=http://aiostreams:3000"), "http://other/x")
        # A rule whose source is only spaces (a stray ", =..." added by hand) rewrites nothing; it prefixed
        # every link the rules before it didn't match
        rules = "https://jf.example=http://aiostreams:3000, =http://other:1"
        self.assertEqual(rewrite("https://cdn.example.net/play/x", rules), "https://cdn.example.net/play/x")
        self.assertEqual(rewrite("https://jf.example/x", rules), "http://aiostreams:3000/x")
        self.assertEqual(rewrite("https://cdn.example.net/x", "https://cdn.example.net= "), "https://cdn.example.net/x")


class SourcesTest(unittest.TestCase):
    def row(self, **kw):
        d = {"type": "debrid", "service": {"id": "torbox", "cached": True}, "addon": {"name": "Torrentio"},
             "filename": "a.mkv", "size": 10}
        d.update(kw)
        return {"url": "http://x/y", "streamData": d, "behaviorHints": {}}

    def test_unreadable_rows(self):
        self.assertIsNone(unreadable(self.row()))
        self.assertEqual(unreadable(self.row(type="usenet")), "type usenet")
        self.assertEqual(unreadable(self.row(service={"id": "torbox", "cached": False})), "not cached")
        self.assertEqual(unreadable(self.row(library=True)), "library row")
        self.assertIsNone(unreadable(self.row(addon={"name": "Private Map"})))      # nothing skipped by default
        self.assertEqual(unreadable(self.row(addon={"name": " Private Map "}), ("private map",)), "addon Private Map")
        self.assertEqual(Aios("", skip_addons=[" Private Map", ""]).skip, ("private map",))
        self.assertEqual(Settings({"SUBSYNC_SKIP_ADDONS": "Private Map, Other"}).skip_addons, ("Private Map", "Other"))

    def test_a_config_client_takes_the_services_settings(self):
        # One place builds the service's clients of its configs (self-hosted and public mode alike)
        s = Settings({"SUBSYNC_SKIP_ADDONS": "Private Map", "SUBSYNC_SIZE_SLACK_MB": "2"})
        a = Aios.from_settings(s, "https://aios.test/stremio/u1/E1/manifest.json", "p1", "https://subs.test/")
        self.assertEqual((a.base, a.password, a.own, a.skip, a.size_slack),
                         ("https://aios.test/stremio/u1/E1", "p1", ("https://subs.test",), ("private map",),
                          2 * 1024 * 1024))

    def test_extras_round_trip(self):
        h = {"videoHash": "0123456789abcdef", "videoSize": 123, "filename": "A B [x].mkv"}
        self.assertEqual(parse_extras(extras(h)), h)

    def test_extras_are_split_before_each_value_is_decoded(self):
        # An "&", a "%" or a "+" in a file name stays in it; "+" between words is a space (form encoding);
        # Stremio's own form, the whole segment encoded once more, reads the same
        tom = {"videoSize": 1000, "filename": "Tom & Jerry.mkv"}
        self.assertEqual(parse_extras("videoSize=1000&filename=Tom%20%26%20Jerry.mkv"), tom)
        self.assertEqual(parse_extras("videoSize=1000&filename=Tom+%26+Jerry.mkv"), tom)
        self.assertEqual(parse_extras(urllib.parse.quote("videoSize=1000&filename=Tom+%26+Jerry.mkv", safe="")), tom)
        self.assertEqual(parse_extras("filename=100%25AB.mkv"), {"filename": "100%AB.mkv"})
        self.assertEqual(parse_extras("filename=C%2B%2B.mkv"), {"filename": "C++.mkv"})
        for h in ({"filename": "Law.&.Order.S01E01.mkv", "videoSize": 5}, {"filename": "100%AB+1 =2.mkv"}):
            self.assertEqual(parse_extras(extras(h)), h)
        self.assertEqual(parse_extras(""), {})
        self.assertEqual(parse_extras("videoSize=x&other=1&filename="), {"filename": ""})

    def test_a_size_in_other_digits_is_no_size(self):
        # Isdigit took "²", which int refuses (the request failed), and read other scripts' digits
        # and int() refuses more than 4,300 digits: such a size failed the request too
        for size in ("\u00b2", "\u0661\u0662", "\uff11\uff12", "1\u00b2", "1" * 5000):
            with self.subTest(size=size[:8]):
                self.assertEqual(parse_extras("filename=a.mkv&videoSize=" + urllib.parse.quote(size)),
                                 {"filename": "a.mkv"})

    def test_an_id_is_one_path_segment_of_aiostreams_address(self):
        # A "/" in an id ("%2F" in the request) went on into the listing's address as a path
        a = Aios("http://aios/stremio/u1/E1")
        a.check_config = lambda: None
        asked = []
        with mock.patch.object(sources, "_get_json", lambda url, timeout: asked.append(url) or {}):
            a.streams("movie", "tt5/../../../../api/v1/user")
            a.subtitles("series", "tt5/../x", {"videoSize": 3})
            a.streams("series", "tt1:2:3")
        self.assertEqual(asked, ["http://aios/stremio/u1/E1/stream/movie/tt5%2F..%2F..%2F..%2F..%2Fapi%2Fv1%2Fuser"
                                 ".json", "http://aios/stremio/u1/E1/subtitles/series/tt5%2F..%2Fx/videoSize=3.json",
                                 "http://aios/stremio/u1/E1/stream/series/tt1:2:3.json"])


class FindRowTest(unittest.TestCase):
    """The played file is found when its listed size is rounded or wrong. Stremio sends the real
    size, listings round it to 0.01 GiB (up to 5.4 MB off), Torrentio listed 1,679 bytes for a 3 GB file; a row whose
    listing names the file's hash is that file. The file's own hash, read with its index, settles it (process())."""
    REAL = 1_438_814_044
    NAME = "Show.S01E01.WEB-DL.mkv"

    def aios(self, *rows):
        a = Aios("http://aios", size_slack=5_767_168)
        a.streams = lambda *_: list(rows)
        return a

    @staticmethod
    def row(name, size, url="http://x/1", video_hash=None):
        return {"url": url, "streamData": {"type": "debrid", "service": {"id": "torbox", "cached": True},
                                           "filename": name, "size": size},
                "behaviorHints": {"videoHash": video_hash} if video_hash else {}}

    def find(self, hints, *rows):
        got = self.aios(*rows).find_row("series", "tt1:1:1", hints)
        return got["url"] if got else None

    def test_the_real_size_finds_a_rounded_listing(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": self.REAL},
                                   self.row(self.NAME, self.REAL + 3_000_000)), "http://x/1")

    def test_an_exact_size_wins_over_a_close_one(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": self.REAL},
                                   self.row(self.NAME, self.REAL + 3_000_000, "http://x/1"),
                                   self.row(self.NAME, self.REAL, "http://x/2")), "http://x/2")

    def test_a_listed_size_no_video_can_have_is_found_by_its_name(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": self.REAL}, self.row(self.NAME, 1679)), "http://x/1")

    def test_another_size_of_the_same_name_is_another_file(self):
        self.assertIsNone(self.find({"filename": self.NAME, "videoSize": self.REAL}, self.row(self.NAME, 2 * self.REAL)))

    def test_a_row_listing_the_files_hash_is_that_file(self):
        self.assertEqual(self.find({"videoHash": "AB12", "videoSize": self.REAL},
                                   self.row("other name.mkv", self.REAL, "http://x/1"),
                                   self.row("renamed.mkv", self.REAL, "http://x/2", video_hash="ab12")), "http://x/2")

    def test_a_hash_sent_without_its_leading_zero_finds_its_row(self):
        # The hashes were compared as text, so this request found no row by its hash, and without a file name
        # none at all
        self.assertEqual(self.find({"videoHash": "8e245d9679d31e1", "videoSize": self.REAL},
                                   self.row("other name.mkv", self.REAL, "http://x/1"),
                                   self.row("renamed.mkv", self.REAL, "http://x/2", video_hash="08e245d9679d31e1")),
                         "http://x/2")

    def test_same_name_rows_with_sizes_no_video_has_take_the_first(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": 1679},
                                   self.row(self.NAME, 1679, "http://x/1"), self.row(self.NAME, 1679, "http://x/2")),
                         "http://x/1")

    def test_hashes_compare_by_value(self):
        self.assertTrue(same_hash("0f3a000000000001", "f3a000000000001"))
        self.assertTrue(same_hash("AB12", "ab12"))
        self.assertFalse(same_hash("ab12", "ab13"))
        self.assertFalse(same_hash("not hex", "ab12"))

    def test_expected_size_for_the_reader(self):
        self.assertEqual(expected_size({"videoSize": self.REAL}, self.row(self.NAME, 1679)), self.REAL)
        self.assertEqual(expected_size({"videoSize": 1679}, self.row(self.NAME, self.REAL)), self.REAL)
        self.assertIsNone(expected_size({"videoSize": 1679}, self.row(self.NAME, 1679)))   # neither can be a video


class FakeAios(Aios):
    def __init__(self):
        super().__init__("")
        self.row = None

    def find_row(self, *a):
        return self.row

    def subtitles(self, *a):
        return []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(url, data=None, headers=None, method=None, follow=True):
    """One request's answer: (status, body, headers). An error status is an answer like any other, read and then closed
; with `follow` off, so is a redirect, as a player that doesn't follow it gets it (its Location)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    open_ = urllib.request.urlopen if follow else urllib.request.build_opener(NoRedirect).open
    try:
        with open_(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read(), dict(e.headers)


def no_file_server(test):
    """A file server on this machine that holds no file, closed when the test ends: a read of it fails at once (HTTP
    404, not tried again), and its `headers` record each request made of it."""
    tmp = TempDir()
    test.addCleanup(tmp.__exit__, None, None, None)
    files = RangeServer(tmp.__enter__())
    test.addCleanup(files.close)
    return files


class AddonCase(unittest.TestCase):
    """The self-hosted service's parts over a worker that is not started (its jobs stay queued): make_addon() makes them
    in a data folder of the test's own, serve_addon() serves the app on a port of this machine. Each is closed by the
    test's cleanups, the server first, also when the set-up fails half-way. No tests of its own."""

    def make_addon(self, env, aios=None, clock=time.time):
        tmp = TempDir()
        data = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        self.s = Settings(dict({"SUBSYNC_DATA": data}, **env))
        self.store = closed_after(self, Store(data, clock=clock))
        self.safety = Safety()
        self.worker = Worker(self.s, self.store, FakeAios() if aios is None else aios, self.safety)
        self.app = App(self.s, self.store, self.worker, self.safety)

    def serve_addon(self):
        self.server = start_server(serve(self.app, 0))
        self.addCleanup(stop_server, self.server)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path):
        """A route's answer as a player that doesn't follow redirects gets it: (status, body, headers)."""
        return http(self.base + path, follow=False)


class AddonFixture(AddonCase):
    """The addon's server over a worker that is not started (its jobs stay queued), the audio route off, subtitles
    downloaded from ORIGINALS. No tests of its own: each class below has its own."""

    def setUp(self):
        watch_the_log(self)
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_PENDING_ENTRY": "on",
                         "SUBSYNC_PENDING_WAIT_MS": "200"})
        self.downloads = fake_downloads(self)
        self.serve_addon()


class AddonTest(AddonFixture):
    def test_manifest(self):
        code, body, _ = http(self.base + "/manifest.json")
        m = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(m["resources"], ["subtitles"])

    def test_a_head_is_answered_with_the_headers_only(self):
        # HEAD goes to the GET route, and the answer leaves out the body it announces
        _, body, _ = http(self.base + "/manifest.json")
        got = exchange(self.server.server_address[1],
                       b"HEAD /manifest.json HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        head, _, rest = got.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200 "), head)
        self.assertIn(f"Content-Length: {len(body)}".encode(), head)
        self.assertEqual(rest, b"")

    def test_known_file_lists_labelled_entries_and_serves_them(self):
        # the store's clock, moved by hand after the listing, shows each file request's mark of the copy as served
        # (put writes last_served as created and the listing marks it too, so a file route that stopped
        # marking it went unseen)
        clock = self.store.clock = Clock(time.time())
        ass = ("[Script Info]\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi\n")
        key = self.store.put(FILE, SHA_A, "eng", 0, "synced", body=ass, fmt="ass", candidate_id="os-123")
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})   # read just now: no new job, no pending entry
        code, body, _ = http(self.base + "/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        subs = json.loads(body)["subtitles"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["lang"], "eng")
        self.assertEqual(subs[0]["label"], "English (synced)")
        self.assertEqual(subs[0]["variantOf"], "os-123")      # marked as a variant of the original
        self.assertTrue(subs[0]["url"].startswith(f"http://subsync:8790/file/{ALGO_VERSION}_"))
        path = subs[0]["url"].replace("http://subsync:8790", "")
        clock.t += 60
        code, body, headers = http(self.base + path)
        self.assertEqual(code, 200)
        self.assertIn(b"Dialogue: 0,0:00:01.00", body)
        self.assertEqual(self.store.get(key)["last_served"], clock.t)   # the file route marked it served
        clock.t += 60
        code, body, _ = http(self.base + path.replace(".ass", ".srt"))
        self.assertIn(b"00:00:01,000 --> 00:00:02,000", body)
        self.assertEqual(self.store.get(key)["last_served"], clock.t)   # its SRT form too
        self.assertGreater(self.store.get(key)["last_served"], self.store.get(key)["created"])

    def test_unknown_file_queues_a_job_and_lists_pending_entries(self):
        # subtitle list not known yet: nothing to list (no entry for a language that may have no subtitle at all)
        code, body, _ = http(self.base + "/subtitles/series/tt2%3A1%3A1/filename=b.mkv&videoSize=5.json")
        self.assertEqual(json.loads(body)["subtitles"], [])
        self.assertEqual(len(self.worker.jobs), 1)
        job = next(iter(self.worker.jobs.values()))
        self.assertEqual((job.type, job.video_id), ("series", "tt2:1:1"))
        # the job has its subtitle list (English only): a pending English entry, none for Russian
        self.worker.originals[job.alias] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        t0 = time.time()
        code, body, _ = http(self.base + "/subtitles/series/tt2%3A1%3A1/filename=b.mkv&videoSize=5.json")
        subs = json.loads(body)["subtitles"]
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs], [("eng", "English (synced)", "os-9")])
        self.assertIn("/pending/", subs[0]["url"])
        del self.worker.originals[job.alias]
        # no result and no original known yet: 503 so the player retries
        t0 = time.time()
        code, _, headers = http(self.base + "/pending/" + token(job.alias) + "/eng.srt")
        self.assertEqual(code, 503)
        self.assertLess(time.time() - t0, 2.0)
        # the original known: its text, downloaded and checked, not a redirect to it
        self.worker.originals[job.alias] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        code, body, headers = http(self.base + "/pending/" + token(job.alias) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertNotIn("Location", headers)
        # an original in another language: the one-line note, not the original
        self.downloads.files["http://opensubs/x.srt"] = dialogue_srt("spa").encode()
        with self.worker.cv:
            self.worker.fetched_subs.clear()
        code, body, _ = http(self.base + "/pending/" + token(job.alias) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"No English subtitle was found for this file.", body)

    def test_every_srt_link_sends_srt_whatever_the_copys_format(self):
        # An OpenSubtitles V3+ subtitle (WebVTT) in sync as it is is listed unchanged, and its .srt link, its
        # pick entry and its pending entry sent the WebVTT text as SRT: players that read SRT strictly show nothing
        alias = "name:v.mkv|1000"
        key = self.store.put(FILE, SHA_A, "eng", 0, "synced", detail={"unchanged": True}, fmt="srt",
                             body="WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHello\n", candidate_id="osp-1")
        self.store.alias(alias, FILE)
        srt = "1\n00:00:01,000 --> 00:00:02,500\nHello\n"
        for path in (f"/file/{file_id(key)}.srt", f"/pick/{token(alias)}/eng.srt", f"/pending/{token(alias)}/eng.srt"):
            with self.subTest(path=path):
                code, body, headers = http(self.base + path)
                self.assertEqual((code, body.decode(), headers["Content-Type"]),
                                 (200, srt, "application/x-subrip; charset=utf-8"))

    def test_a_pending_entry_answers_as_the_pick_entry_does(self):
        # After a restart, or for a file subsync knows nothing of, a pending entry answered "busy, try again in
        # 1 second" on every try, where the pick entry answers "not found", and never looked at a copy stored for its
        # file; now both answer alike in each state (a file worked on: test_unknown_file_queues_a_job_and_lists_...)
        alias = "name:p.mkv|1000"
        routes = [f"/{route}/{token(alias)}/eng.srt" for route in ("pending", "pick")]
        self.assertEqual([http(self.base + path)[0] for path in routes], [404, 404])    # nothing known of the file
        self.store.alias(alias, FILE)                    # read before (its list not known any more): the player retries
        for path in routes:
            with self.subTest(path=path):
                code, _, headers = http(self.base + path)
                self.assertEqual((code, headers.get("Retry-After")), (503, "1"))
        text = "1\n00:00:01,000 --> 00:00:02,000\nstored\n"
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body=text, fmt="srt")    # a copy stored before the restart
        for path in routes:
            with self.subTest(path=path):
                code, body, headers = http(self.base + path)
                self.assertEqual((code, body.decode(), headers.get(SERVED)), (200, text, "copy"))
        self.assertEqual(len(self.worker.jobs), 0)

    def test_no_hints_no_job(self):
        code, body, _ = http(self.base + "/subtitles/movie/tt3.json")
        self.assertEqual(json.loads(body)["subtitles"], [])
        self.assertEqual(len(self.worker.jobs), 0)

    def test_unlisted_file_is_not_looked_for_again(self):
        self.worker.unlisted["name:e.mkv|9"] = time.time()
        http(self.base + "/subtitles/movie/tt6/filename=e.mkv&videoSize=9.json")
        self.assertEqual(len(self.worker.jobs), 0)
        http(self.base + "/subtitles/movie/tt6/filename=other.mkv&videoSize=9.json")
        self.assertEqual(len(self.worker.jobs), 1)                       # another file of the title is looked for

    def test_no_subtitle_to_sync_reads_nothing(self):
        # the file is listed, but no subtitle is, or none in English: nothing of it is asked for (listed at an
        # address that never answers, a read counted no bytes and went unseen)
        files = no_file_server(self)
        self.worker.aios.row = {"url": files.url("f.mkv")}
        for name, listed in (("f.mkv", []), ("g.mkv", [{"id": "os-s", "url": "http://opensubs/s.srt", "lang": "spa"}])):
            with self.subTest(listed=listed):
                self.worker.aios.subtitles = lambda *a, listed=listed: [dict(c) for c in listed]
                self.worker.process(Job("movie", "tt7", {"filename": name, "videoSize": 9}))
                self.assertEqual(files.headers, [])
                self.assertEqual(self.store.day_count("read"), 0)


class PickTest(AddonFixture):
    """Audio route on: it starts only when the viewer downloads subsync's own entry for the file."""
    URL = "/subtitles/movie/tt9/filename=b.mp4&videoSize=5.json"
    ALIAS = "name:b.mp4|5"

    def setUp(self):
        super().setUp()
        self.s.audio_route = True
        self.assertEqual(self.s.audio_choice, "pick")
        self.job = Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
        self.assertEqual(self.job.alias, self.ALIAS)

    def waiting(self):
        self.worker.pickable[self.ALIAS] = self.job
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}

    def test_a_plain_request_may_not_use_the_audio_route(self):
        self.assertFalse(self.worker.audio_allowed(self.job))
        picked = Job("movie", "tt9", self.job.hints)
        picked.audio = True
        self.assertTrue(self.worker.audio_allowed(picked))
        self.s.audio_when = "open"       # as soon as it is opened is an audio job too, never the first pass
        self.assertFalse(self.worker.audio_allowed(self.job))
        self.assertTrue(self.worker.audio_allowed(picked))
        self.s.audio_route = False
        self.assertFalse(self.worker.audio_allowed(picked))

    def test_a_file_waiting_for_a_pick_lists_one_subsync_entry_per_language_that_has_a_subtitle(self):
        self.waiting()
        self.store.file_read("f" * 16 + "-5", 5, "f" * 16, False, {})   # read a moment ago: no new job
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs], [("eng", "English (Subsync)", "os-9")])
        self.assertTrue(subs[0]["url"].endswith("/pick/" + token(self.ALIAS) + "/eng.srt"))
        self.s.audio_route = False                  # nothing waits for a pick any more: its original, unchanged
        self.assertEqual([s["label"] for s in json.loads(http(self.base + self.URL)[1])["subtitles"]],
                         ["English (original)"])

    def test_the_first_answer_is_not_held_back(self):
        self.s.pending_entry = False
        self.s.pending_wait = 3.0
        t0 = time.time()
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertLess(time.time() - t0, 1.0)             # AIOStreams orders addons by who answers first
        self.assertEqual([s["label"] for s in subs], ["English (Subsync)"])

    def test_public_mode_names_the_entry_aiosubtitles_and_self_hosted_keeps_subsync(self):
        self.s.public = True                                   # the public service's own name
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertEqual([s["label"] for s in subs], ["English (AIOSubtitles)"])
        self.s.public = False
        self.assertEqual([s["label"] for s in json.loads(http(self.base + self.URL)[1])["subtitles"]],
                         ["English (Subsync)"])

    def test_the_first_answer_already_has_the_entry_for_every_language_and_the_download_waits_for_the_list(self):
        self.s.pick_wait = 3.0
        self.s.languages = type(self.s.languages)(["eng", "rus"])
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]     # unknown file: a job is queued
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs],
                         [("eng", "English (Subsync)", "original"), ("rus", "Russian (Subsync)", "original")])
        self.assertEqual([j.audio for j in self.worker.jobs.values()], [False])

        def list_arrives():   # the job gets the list, reads the index: no subtitle track (the file needs the audio route)
            time.sleep(0.4)
            with self.worker.cv:
                self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
                self.worker.jobs.pop(self.ALIAS)
                self.worker.queue.clear()
                self.worker.done[self.ALIAS] = {"eng": None, "rus": None}
                self.worker.cv.notify_all()
        threading.Thread(target=list_arrives, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertLess(time.time() - t0, 2.5)
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")   # no Russian subtitle exists: a note
        self.assertEqual(code, 200)
        self.assertIn(b"No Russian subtitle was found for this file", body)
        self.assertIn(self.ALIAS, self.worker.picked_early)                              # remembered for the index read

    def test_downloading_the_entry_serves_the_original_and_starts_the_audio_route_once(self):
        self.waiting()
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))          # its text, checked
        self.assertNotIn("Location", headers)
        self.assertNotIn(self.ALIAS, self.worker.pickable)
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.get("/pick/" + token(self.ALIAS) + "/eng.srt")           # again: nothing more is queued
        self.assertEqual(len(self.worker.jobs), 1)

    def test_a_file_put_off_by_a_stop_holds_no_wait(self):
        # A job stopped by the day's budget or a back-off is queued again SUBSYNC_RETRY_STOPPED_S ahead and
        # gives nothing before then: a pick serves the original at once (it waited 3 s of 6 here, 11 of 14 by default)
        # and a listing's pending wait ends at once; the file keeps its entries
        self.s.pick_wait, self.s.pending_wait = 6.0, 2.0
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        put_off = Job(self.job.type, self.job.video_id, self.job.hints)
        put_off.not_before = time.time() + 600
        self.worker._requeue(put_off)                           # as a stopped job is
        self.assertTrue(self.worker.busy_with(self.ALIAS))
        self.assertFalse(self.worker.busy_reading(self.ALIAS))
        t0 = time.time()
        self.assertEqual(self.worker.wait_for(self.ALIAS, "eng", 2.0, final=True), (None, "http://opensubs/x.srt"))
        self.assertEqual(self.worker.wait_for_originals(self.ALIAS, 2.0), self.worker.originals[self.ALIAS])
        self.assertLess(time.time() - t0, 0.5)
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertLess(time.time() - t0, 1.0)
        t0 = time.time()
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual([(s["lang"], s["label"]) for s in subs], [("eng", "English (synced)")])   # its pending entry
        self.assertEqual(list(self.worker.jobs.values()), [put_off])
        put_off.not_before = 0                                  # due: waits are for it again
        self.assertTrue(self.worker.busy_reading(self.ALIAS))
        t0 = time.time()
        self.worker.wait_for(self.ALIAS, "eng", 0.3, final=True)
        self.assertGreaterEqual(time.time() - t0, 0.25)

    def test_a_language_whose_subtitles_are_all_unwanted_gets_no_entry(self):
        # With SUBSYNC_SKIP_AI on, a forced, AI-made or machine-translated subtitle is never the original a
        # pick or "(original)" entry serves, with SUBSYNC_RANK_BY_RELEASE off too: a language that has only such ones gets
        # no entry once its list is known
        self.s.rank_by_release = False
        self.s.languages = type(self.s.languages)(["eng", "rus"])
        self.worker._set_originals(self.ALIAS, [
            {"id": "forced", "lang": "eng", "url": "http://opensubs/y.srt", "release": "Movie.Forced"},
            {"id": "os-9", "lang": "eng", "url": "http://opensubs/x.srt", "release": "Movie"},
            {"id": "os-r", "lang": "rus", "url": "http://opensubs/r.srt", "release": "Movie", "ai": True}], "b.mp4", None)
        self.store.file_read("f" * 16 + "-5", 5, "f" * 16, False, {})   # read a moment ago: no new job
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        listed = lambda: [(e["lang"], e["label"], e["variantOf"])   # noqa: E731
                          for e in json.loads(http(self.base + self.URL)[1])["subtitles"]]
        self.s.audio_route = False                              # nothing waits for a pick: its "(original)" entries
        self.assertEqual(listed(), [("eng", "English (original)", "os-9")])
        self.s.audio_route = True
        self.worker.pickable[self.ALIAS] = self.job             # it waits for a pick: its "(Subsync)" entries
        self.assertEqual(listed(), [("eng", "English (Subsync)", "os-9")])
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")   # an entry listed before: the note
        self.assertEqual(code, 200)
        self.assertIn(b"No Russian subtitle was found for this file.", body)

    def test_a_waiting_file_serves_the_anchored_guess_of_another_language(self):
        self.waiting()
        self.worker.originals[self.ALIAS]["rus"] = {"url": "http://opensubs/r.srt", "id": "os-r"}
        self.worker.provisional[self.ALIAS] = {"rus": "1\n00:00:01,000 --> 00:00:02,000\nanchored\n"}
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")
        self.assertEqual((code, b"anchored" in body), (200, True))
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")   # the main language: its best guess
        self.assertEqual((code, body.decode()), (200, X_TEXT))

    def test_the_audio_verdict_replaces_the_anchored_guess(self):
        self.waiting()
        self.worker.provisional[self.ALIAS] = {"rus": "anchored", "eng": "x"}
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        job.audio = True
        self.worker._finish(job, "rus", "k-rus", {})
        self.assertEqual(self.worker.provisional[self.ALIAS], {"eng": "x"})
        self.assertEqual(self.worker.published(self.ALIAS, "rus"), "k-rus")

    def test_anchored_guesses_can_be_switched_off(self):
        self.assertTrue(self.s.anchored_guess)
        self.assertFalse(Settings({"SUBSYNC_ANCHORED_GUESS": "off"}).anchored_guess)

    def test_a_waiting_language_is_published_with_its_anchored_guess_when_another_raises(self):
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        job.fetched = {"eng": []}
        calls = []
        self.worker._set_provisional = lambda j: calls.append(j)

        def sync(lang, cands):
            if lang == "rus":
                raise ValueError("bad subtitle")
            return "k-" + lang
        with self.assertRaises(ValueError):
            self.worker._sync_all(job, {"eng": [], "rus": []}, sync)
        self.assertEqual(calls, [job])
        self.assertEqual(self.worker.published(self.ALIAS, "eng"), "k-eng")
        self.assertIn("eng", self.worker.judged[self.ALIAS])

    def cands(self):
        return {"eng": [{"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"},
                        {"id": "os-8", "url": "http://opensubs/y.srt", "lang": "eng"}],
                "rus": [{"id": "os-r", "url": "http://opensubs/r.srt", "lang": "rus"}]}

    def test_a_no_track_file_picked_already_hands_over_at_once_and_its_pick_keeps_waiting(self):
        # 12 Angry Men (YIFY MP4): the audio route started 15 s after the pick, once ~40 downloads had been judged for
        # nothing, and the pick was released with the original as that pass ended
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.worker.picked_early[self.ALIAS] = time.time()
        fetched = []
        self.worker._fetch_all = lambda job, lang, tries: (fetched.append((lang, [c["id"] for _, c in tries])),
                                                           [(r, c, b"1\n00:00:01,000 --> 00:00:02,000\nx\n") for r, c in tries])[1]
        provisional = []
        self.worker._set_provisional = lambda job: provisional.append(sorted(job.fetched))
        self.worker._await_pick(self.job, self.cands(), "b.mp4", None)
        # the guess, then the main language's first tries for a consensus, and the other's first tries
        self.assertEqual(sorted(fetched), [("eng", ["os-9", "os-8"]), ("rus", ["os-r"])])
        self.assertEqual(provisional, [["eng", "rus"]])
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.assertNotIn(self.ALIAS, self.worker.judged)                              # the pick keeps waiting

    def test_a_no_track_file_not_picked_yet_waits_for_its_pick(self):
        self.s.anchored_guess = False
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        fetched = []
        self.worker._fetch_all = lambda job, lang, tries: (fetched.append((lang, [c["id"] for _, c in tries])), [])[1]
        self.worker._set_provisional = lambda job: self.fail("no other language lined up without anchored guesses")
        self.worker._await_pick(self.job, self.cands(), "b.mp4", None)
        # Without anchored guesses too, the main language's guess and its first tries, for a consensus
        self.assertEqual(fetched, [("eng", ["os-9", "os-8"])])
        self.assertIn(self.ALIAS, self.worker.pickable)
        self.assertEqual(len(self.worker.jobs), 0)
        self.assertEqual(self.worker.judged[self.ALIAS], {"eng", "rus"})

    def test_a_no_track_files_languages_are_downloaded_at_once(self):
        # The first pass of a file with no track downloaded its languages one after the other, so the audio
        # job a pick queued (it starts once this pass ends) waited for every language's rounds in turn; with
        # SUBSYNC_PARALLEL_LANGUAGES (the default) they go at once, as LanguageSync's downloads do
        self.worker._set_provisional = lambda job: None
        for parallel, expected in ((True, ["eng", "rus"]), (False, [])):
            with self.subTest(parallel=parallel):
                self.s.parallel_languages = parallel
                self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
                both, together = threading.Barrier(2, timeout=2 if parallel else 0.2), []

                def fetch_all(job, lang, tries, both=both, together=together):
                    try:
                        both.wait()                              # the other language's downloads run meanwhile
                        together.append(lang)
                    except threading.BrokenBarrierError:
                        pass
                    return [(r, c, b"1\n00:00:01,000 --> 00:00:02,000\nx\n") for r, c in tries]
                self.worker._fetch_all = fetch_all
                job = Job(self.job.type, self.job.video_id, self.job.hints)
                self.worker._await_pick(job, self.cands(), "b.mp4", None)
                self.assertEqual(sorted(together), expected)
                self.assertEqual({lang: [c["id"] for c, _ in got] for lang, got in job.fetched.items()},
                                 {"eng": ["os-9", "os-8"], "rus": ["os-r"]})

    def test_a_file_waiting_for_its_pick_keeps_none_of_its_first_pass_downloads(self):
        # The file waited as its first pass's own job, whose downloaded texts (the guess and its consensus
        # tries, SUBSYNC_GATE_TRIES per other language) stayed in memory for as long as it waited; a pick needs the
        # file's type, id and hints only
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        text = b"1\n00:00:01,000 --> 00:00:02,000\nx\n"
        self.worker._fetch_all = lambda job, lang, tries: [(r, c, text) for r, c in tries]
        first = Job(self.job.type, self.job.video_id, self.job.hints)
        self.worker._await_pick(first, self.cands(), "b.mp4", None)
        self.assertEqual(sorted(first.fetched), ["eng", "rus"])                  # the pass had them, for its guesses
        waiting = self.worker.pickable[self.ALIAS]
        self.assertIsNot(waiting, first)
        self.assertEqual(waiting.fetched, {})
        self.assertEqual((waiting.type, waiting.video_id, waiting.hints, waiting.audio),
                         (first.type, first.video_id, first.hints, False))

    def test_a_picked_files_audio_job_waits_for_its_text_pass(self):
        text = Job(self.job.type, self.job.video_id, self.job.hints)
        self.worker.current = text                                                # the text pass still runs
        picked = Job(self.job.type, self.job.video_id, self.job.hints)
        picked.audio = True
        self.worker.submit(picked)
        got = []
        t = threading.Thread(target=lambda: got.append(self.worker._next("audio")), daemon=True)
        self.addCleanup(self.worker.stop)       # the lane's wait ends as the test does, as it passes or not
        t.start()
        t.join(0.3)
        self.assertEqual(got, [])                                                # never both at once on one file
        with self.worker.cv:
            self.worker.current = None                                            # the text pass ends
            self.worker.cv.notify_all()
        t.join(6.0)
        self.assertEqual(got, [picked])

    def test_a_language_the_audio_route_judged_gets_no_anchored_guess(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        main = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
        job.fetched = {"eng": [({"id": "os-9"}, main)], "rus": [({"id": "os-r"}, main)]}
        self.worker.judged[self.ALIAS] = {"rus"}                                 # the audio route's verdict came first
        import subsync.pipeline as pl
        real = pl.anchored_guess
        pl.anchored_guess = lambda main_text, texts: {"index": 0, "body": "anchored", "share": 0.9}
        try:
            self.worker._set_provisional(job)
        finally:
            pl.anchored_guess = real
        self.assertEqual(self.worker.provisional[self.ALIAS], {})

    def test_the_anchored_guesses_check_no_language_again(self):
        # job.fetched holds only texts the language check passed (_check_fetched, LanguageSync._wrong_language:
        # test_worker_sources' LanguageCheckWorkerTest), and checking them again took about 38 ms a text, 1.6 s on the
        # way to the audio route's start with three languages
        from subsync import langcheck
        import subsync.pipeline as pl
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        main = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
        job.fetched = {"eng": [({"id": "os-9"}, main)], "rus": [({"id": "os-r"}, main), ({"id": "os-s"}, main)]}
        measured = []
        real = langcheck.measure
        with mock.patch.object(langcheck, "measure", lambda *a, **k: measured.append(1) or real(*a, **k)), \
                mock.patch.object(pl, "anchored_guess", lambda main_text, texts: {"index": 1, "body": "anchored",
                                                                                 "share": 0.9}):
            self.worker._set_provisional(job)
        self.assertEqual(self.worker.provisional[self.ALIAS], {"rus": "anchored"})
        self.assertEqual(measured, [])

    def test_an_anchored_guess_is_logged_without_its_subtitles_id(self):
        # A subtitle a config lists without an id has its address as its id, and the log never holds a
        # config's address: the guess is named by its place among the downloads and its source
        from unittest import mock
        import subsync.pipeline as pl
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        main = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
        address = "https://subs.example/eyJjb25maWciOiJteSBvd24ifQ/subtitles/movie/tt9.srt"
        job.fetched = {"eng": [({"id": "os-9"}, main)],
                       "rus": [({"id": address, "url": address, "source": "config"}, main)]}
        guess = {"index": 0, "body": "anchored", "share": 0.9}
        with mock.patch.object(pl, "anchored_guess", lambda main_text, texts: guess), \
                self.assertLogs("subsync.worker", "INFO") as logs:
            self.worker._set_provisional(job)
        self.assertEqual(self.worker.provisional[self.ALIAS], {"rus": "anchored"})
        line = next(line for line in logs.output if "anchored" in line)
        self.assertIn("candidate 1 of 1 (config) lines up with the eng guess", line)
        self.assertNotIn("subs.example", line)
        self.assertNotIn("http", line)

    def test_a_refused_read_in_one_language_stops_the_others_reads_at_once(self):
        # A job's languages judged in parallel read through the same account: the back-off starts when one
        # language's read is refused, so the other's next read stops too, not only once every language ended
        from subsync.reader import Refused
        self.s.parallel_languages = True
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        guard = JobGuard(self.safety, self.store, 10 ** 12)
        refused, after = threading.Event(), []

        def sync(lang, cands):
            if lang == "eng":
                time.sleep(0.2)                      # the other language reads meanwhile
                refused.set()
                raise Refused("HTTP 429")
            for _ in range(20):                      # a read every 50 ms, each checked by the job's guard
                guard.before_read(1)
                after.append(refused.is_set())
                guard.after_read(1)
                time.sleep(0.05)
            return "k-spa"
        with self.assertRaises(Refused):
            self.worker._sync_all(job, {"eng": [], "spa": []}, sync)
        self.assertTrue(self.safety.backing_off())
        self.assertLessEqual(sum(after), 1)          # at most a read checked as the refusal came
        self.assertEqual(self.safety.refusals, 1)    # the back-off started once for the job

    def test_a_refusal_ends_the_job_as_refused_whatever_the_other_language_raised(self):
        # The job's end is handled as a refusal (the back-off, the file's own) when another language raised
        # something else first
        from subsync.reader import Refused
        self.s.parallel_languages = True
        job = Job(self.job.type, self.job.video_id, self.job.hints)

        def sync(lang, cands):
            if lang == "eng":
                raise ValueError("bad subtitle")
            time.sleep(0.2)
            raise Refused("HTTP 403")
        with self.assertRaises(Refused):
            self.worker._sync_all(job, {"eng": [], "spa": []}, sync)
        self.assertTrue(self.safety.backing_off())

    def test_two_languages_that_need_the_audio_route_decide_once(self):
        # A file whose track is too small: its languages, judged in parallel, each come to the decision; with
        # a pick come during the first pass, its audio job is queued, and the file doesn't also wait for a pick
        self.worker.picked_early[self.ALIAS] = time.time()
        decided = threading.Event()

        def room(alias):                             # the pick's language holds its decision while the other may decide
            decided.wait(0.5)
            return True
        self.worker._audio_room = room
        got = []

        def decide():
            got.append(self.worker._needs_pick(self.job))
            decided.set()
        langs = [threading.Thread(target=decide, daemon=True) for _ in range(2)]
        for t in langs:
            t.start()
            time.sleep(0.1)
        for t in langs:
            t.join(3)
        self.assertEqual(got, [True, True])
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.assertNotIn(self.ALIAS, self.worker.pickable)

    def test_an_original_replaced_after_its_download_failed_says_so(self):
        # The log names why an original was replaced: its download failed, or its text is in another language
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        cands = [{"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"},
                 {"id": "os-8", "url": "http://opensubs/y.srt", "lang": "eng"}]
        with self.assertLogs("subsync.worker", "INFO") as logs:
            self.worker._recheck_original(self.ALIAS, "eng", cands, {}, set(), "b.mp4", None, failed={"os-9"})
        self.assertEqual(self.worker.originals[self.ALIAS]["eng"]["id"], "os-8")
        self.assertIn("original eng: its download failed, another served instead", logs.output[-1])
        with self.assertLogs("subsync.worker", "INFO") as logs:
            self.worker._recheck_original(self.ALIAS, "eng", cands, {}, {"os-8"}, "b.mp4", None)
        self.assertIn("original eng: not English text, another served instead", logs.output[-1])

    def test_the_fast_paths_downloads_go_fetch_at_once_at_a_time(self):
        # The fast path's downloads keep to FETCH_AT_ONCE, as best fit's do
        from unittest import mock
        from subsync import worker as worker_mod
        running, most, lock = [0], [0], threading.Lock()

        def fetch(url, timeout=30, headers=None):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.1)
            with lock:
                running[0] -= 1
            return X_TEXT.encode()
        tries = [(r, {"id": f"s{r}", "url": f"http://subs.test/{r}.srt", "lang": "eng"}) for r in range(6)]
        with mock.patch.object(worker_mod, "FETCH_AT_ONCE", 2), mock.patch.object(worker_mod, "fetch_subtitle", fetch):
            got = self.worker._fetch_all(self.job, "eng", tries)
        self.assertEqual([rank for rank, _, _ in got], list(range(6)))
        self.assertEqual(most[0], 2)

    def test_the_audio_job_reuses_its_text_pass_list_and_downloads(self):
        calls = []

        class Listing(FakeAios):
            def subtitles(self, *a):
                calls.append(a)
                return [{"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"}]
        self.worker.aios = Listing()
        self.assertEqual(len(self.worker._candidates(self.job, {})), 1)
        audio = Job(self.job.type, self.job.video_id, self.job.hints)
        audio.audio = True
        self.assertEqual(self.worker._candidates(audio, {})[0]["id"], "os-9")
        self.assertEqual(len(calls), 1)                                    # not asked again
        self.worker._candidates(self.job, {})                              # a text pass asks again
        self.assertEqual(len(calls), 2)
        import subsync.worker as w
        got, real = [], w.fetch_subtitle
        w.fetch_subtitle = lambda url, headers=None: (got.append(url), b"sub")[1]
        try:
            cand = {"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"}
            self.assertEqual(self.worker._fetch_one(self.job, "eng", 0, cand)[2], b"sub")
            self.assertEqual(self.worker._fetch_one(audio, "eng", 0, cand)[2], b"sub")
        finally:
            w.fetch_subtitle = real
        self.assertEqual(got, ["http://opensubs/x.srt"])

    def test_a_download_before_the_file_is_known_to_need_audio_is_remembered(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        self.assertEqual(self.get("/pick/" + token(self.ALIAS) + "/eng.srt")[0], 200)
        self.assertEqual(len(self.worker.jobs), 0)
        self.assertIn(self.ALIAS, self.worker.picked_early)
        self.worker._needs_pick(self.job)                              # the index read ends: no subtitle track
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.assertNotIn(self.ALIAS, self.worker.pickable)

    def test_a_pick_while_the_index_read_ends_is_not_lost(self):
        # found in review: the early pick was read unlocked and dropped later under the lock; a pick recorded
        # in between was lost, and the file waited for a pick that had come
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        real = self.worker._may_open

        def pick_meanwhile(job):   # the viewer picks while _needs_pick decides: the file isn't waiting yet
            self.assertFalse(self.worker.pick(self.ALIAS))
            return real(job)
        self.worker._may_open = pick_meanwhile
        self.assertTrue(self.worker._needs_pick(self.job))
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.assertNotIn(self.ALIAS, self.worker.pickable)
        self.assertNotIn(self.ALIAS, self.worker.picked_early)

    def test_a_synced_copy_is_served_from_the_same_link(self):
        self.waiting()
        copy = "1\n00:00:01,000 --> 00:00:02,000\nthe copy\n"
        key = self.store.put("f" * 16 + "-5", SHA_A, "eng", 0, "synced", body=copy, fmt="srt")
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"00:00:01,000", body)
        # the copy, not the original the pick serves otherwise, whose first line has the same time
        self.assertEqual((body.decode(), headers.get(SERVED)), (copy, "copy"))
        self.assertIsNotNone(key)

    def test_a_language_without_any_subtitle_gets_a_one_line_note_not_an_error(self):
        # Stock AIOMetadata turned subsync's 404 into an HTTP 502 for the viewer
        self.waiting()
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/x-subrip"))
        text = body.decode("utf-8")
        self.assertIn("-->", text)
        self.assertIn("No Russian subtitle was found for this file.", text)
        self.assertEqual(len(self.worker.jobs), 1)                     # the English pick is unaffected

    def test_a_list_still_unknown_after_the_wait_asks_to_retry(self):
        self.s.pick_wait = 0.3
        http(self.base + self.URL)                                     # unknown file: a job is queued, list unknown
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Retry-After")), (503, "1"))   # retriable, unlike a note kept an hour

    def test_a_pick_waits_the_pick_wait_in_total_never_more(self):
        # The result wait and the list wait each took the whole wait (up to 28 s), past AIOMetadata's 15 s
        # subtitle timeout, and the viewer got HTTP 502
        self.s.pick_wait = 1.0
        http(self.base + self.URL)                                     # unknown file: an index-only job, nothing answers
        t0 = time.time()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Retry-After")), (503, "1"))
        self.assertLess(time.time() - t0, 1.5)

    def test_a_download_while_best_fit_judges_gets_the_best_copy_so_far(self):
        # Passes are published before every subtitle is judged; the final best slot is written at the end.
        # Sherlock S01E01's first download was the first pass (Russian 62 % within 200 ms) where a better one (82 %) came
        # seconds later: the download waits for the language's final result within the pick wait, and is served the best
        # pass published by then
        self.s.pick_wait = 3.0
        http(self.base + self.URL)                                     # unknown file: an index-only job is queued
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        with self.worker.cv:   # the pass downloaded the original first: the pick may wait its whole deadline
            self.worker.fetched_subs["http://opensubs/x.srt"] = (time.time(), X_TEXT.encode())
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        keys = [self.store.put("f" * 16 + "-5", sha, "eng", rank, "synced", fmt="srt", offer=False,
                               body=f"1\n00:00:01,000 --> 00:00:02,000\n{label}\n")
                for sha, rank, label in ((SHA_A, 3, "first pass"), (SHA_B, 5, "better pass"))]

        def judging():
            time.sleep(0.2)
            self.worker._publish(self.ALIAS, "eng", keys[0])
            time.sleep(0.3)
            self.worker._publish(self.ALIAS, "eng", keys[1])
        threading.Thread(target=judging, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"better pass", body)
        self.assertGreaterEqual(time.time() - t0, 2.5)                 # still judging: it waited the pick wait out

    def test_a_new_job_for_the_file_is_waited_for_not_the_last_ones_result(self):
        # review: a file judged before (a rule change, a list asked again) is queued anew; a pick must wait for the new
        # job's final result, not return at once with the old one
        self.worker._publish(self.ALIAS, "eng", None, final=True)       # the earlier job kept the original
        self.assertIn("eng", self.worker.judged.get(self.ALIAS, set()))   # judged: a pick now wouldn't wait
        self.worker.submit(Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5}))
        self.assertNotIn("eng", self.worker.judged.get(self.ALIAS, set()))

    def test_a_download_is_answered_once_the_language_is_judged(self):
        self.s.pick_wait = 5.0
        http(self.base + self.URL)
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        key = self.store.put("f" * 16 + "-5", SHA_A, "eng", 1, "synced", fmt="srt", offer=False,
                             body="1\n00:00:01,000 --> 00:00:02,000\nthe best\n")

        def judged():
            time.sleep(0.3)
            self.worker._publish(self.ALIAS, "eng", key, final=True)
        threading.Thread(target=judged, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, b"the best" in body), (200, True))
        self.assertLess(time.time() - t0, 2.0)

    def test_a_download_moves_its_files_job_to_the_front(self):
        self.s.pick_wait = 0.3
        self.worker.submit(Job("movie", "tt1", {"filename": "other.mkv", "videoSize": 9}))   # another file asked first
        http(self.base + self.URL)                                     # this file's job, behind it
        self.assertEqual([j.alias for j in self.worker.queue][-1], self.ALIAS)
        self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(self.worker.queue[0].alias, self.ALIAS)

    def test_a_download_names_its_language_to_its_files_job(self):
        # The picked language gets alass sooner in the file's job (Worker.prioritize); it no longer
        # holds the other languages' downloads back
        self.s.pick_wait = 0.3
        calls = []
        self.worker.prioritize = lambda alias, lang=None: calls.append((alias, lang))
        self.get("/pick/" + token(self.ALIAS) + "/rus.srt")
        self.assertEqual(calls, [(self.ALIAS, "rus")])

    def test_a_file_waiting_for_a_pick_is_remembered_after_a_restart(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.worker._needs_pick(self.job)
        self.store.file_read("f" * 16 + "-5", 5, "f" * 16, False, {})   # read a moment ago: no new job
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        restarted = Worker(self.s, self.store, FakeAios(), Safety())   # a new process: nothing in memory
        self.assertTrue(restarted.waits_for_pick(self.ALIAS))
        app = App(self.s, self.store, restarted, Safety())
        subs = app.subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs], [("eng", "English (Subsync)", "os-9")])
        self.assertTrue(restarted.pick(self.ALIAS))
        self.assertEqual([(j.alias, j.audio, j.type, j.video_id, j.hints) for j in restarted.jobs.values()],
                         [(self.ALIAS, True, "movie", "tt9", {"filename": "b.mp4", "videoSize": 5})])
        self.assertFalse(Worker(self.s, self.store, FakeAios(), Safety()).waits_for_pick(self.ALIAS))   # picked once

    def listed(self, *cands):
        """This person's subtitle list for the file, as its text pass got it."""
        with self.worker.cv:
            self.worker.lists[self.ALIAS] = (time.time(), [dict(c) for c in cands])

    def test_a_refused_original_is_replaced_by_the_next_that_passes(self):
        # (Friends S01E01: its "English" original, served by a redirect, was Spanish)
        self.waiting()
        self.downloads.files["http://opensubs/x.srt"] = dialogue_srt("spa").encode()
        self.downloads.files["http://opensubs/y.srt"] = dialogue_srt("eng").encode()
        self.listed({"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"},
                    {"id": "os-8", "url": "http://opensubs/y.srt", "lang": "eng"})
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, dialogue_srt("eng")))
        self.assertNotIn("Location", headers)

    def test_one_downloaded_already_comes_before_a_new_download(self):
        self.waiting()
        self.downloads.files["http://opensubs/x.srt"] = dialogue_srt("spa").encode()
        others = [{"id": f"os-{i}", "url": f"http://opensubs/{i}.srt", "lang": "eng"} for i in range(3)]
        self.listed({"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"}, *others)
        self.downloads.files["http://opensubs/0.srt"] = dialogue_srt("eng").encode()
        with self.worker.cv:     # the file's text pass downloaded the third one a moment ago
            self.worker.fetched_subs["http://opensubs/2.srt"] = (time.time(), dialogue_srt("eng", start=31.0).encode())
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(body.decode(), dialogue_srt("eng", start=31.0))
        self.assertEqual(self.downloads.asked, ["http://opensubs/x.srt"])

    def test_the_note_when_none_passes_after_at_most_three_more_downloads(self):
        self.waiting()
        cands = [{"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"}] + \
            [{"id": f"os-{i}", "url": f"http://opensubs/{i}.srt", "lang": "eng"} for i in range(5)]
        for c in cands:
            self.downloads.files[c["url"]] = dialogue_srt("spa").encode()
        self.listed(*cands)
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"No English subtitle was found for this file.", body)
        self.assertEqual(len(self.downloads.asked), 4)                  # the original and three more

    def test_a_download_past_the_pick_wait_is_a_redirect_to_the_persons_own_original(self):
        # found in review: when the download can't end by the deadline, the original's own address in this
        # person's list, as 1.4.0 sent it, never a 503 (AIOMetadata turns that into an error for the viewer); the pending
        # route too
        self.s.pick_wait = 0.3
        self.waiting()
        release = threading.Event()
        self.addCleanup(release.set)
        self.downloads.hold["http://opensubs/x.srt"] = release
        t0 = time.time()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Location")), (302, "http://opensubs/x.srt"))
        self.assertLess(time.time() - t0, 1.5)
        code, _, headers = self.get("/pending/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Location")), (302, "http://opensubs/x.srt"))
        # a subtitle subsync found itself that a player can't use as it is (a zip): the player asks again instead
        zipped = "https://dl.subdl.com/subtitle/1-2.zip"
        self.listed({"id": "os-9", "url": zipped, "lang": "eng", "source": "subdl"})
        self.downloads.hold[zipped] = release
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Retry-After")), (503, "1"))

    def test_a_late_original_whose_address_cant_go_out_as_it_is_is_a_503(self):
        # The redirect's Location was the address in this person's list as it is: a line break in it wrote a
        # header of its own, a character outside latin-1 broke the answer half-way, and any scheme went out
        self.s.pick_wait = 0.3
        self.waiting()
        release = threading.Event()
        self.addCleanup(release.set)
        for address in ("http://opensubs/x.srt\r\nSet-Cookie: injected=1", "http://opensubs/Ж.srt",
                        "http://opensubs/a b.srt", "ftp://opensubs/x.srt", "javascript:alert(1)"):
            with self.subTest(address=address):
                self.listed({"id": "os-9", "url": address, "lang": "eng"})
                self.downloads.hold[address] = release
                code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
                self.assertEqual((code, headers.get("Retry-After")), (503, "1"))
                self.assertNotIn("Location", headers)
                self.assertNotIn("Set-Cookie", headers)

    def test_a_slow_first_pass_leaves_time_to_download_the_original(self):
        # found in review: the pick spent its whole deadline waiting for the job, and the original, not
        # downloaded yet, got no time: a 503 where 1.4.0 served it
        self.s.pick_wait = 3.5
        http(self.base + self.URL)                                     # unknown file: an index-only job, nothing answers
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        release = threading.Event()
        self.addCleanup(release.set)
        self.downloads.hold["http://opensubs/x.srt"] = release        # a slow source: its download takes a second
        timer = threading.Timer(1.0, release.set)
        self.addCleanup(timer.cancel)
        timer.start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertLess(time.time() - t0, 3.5)

    def test_downloads_of_originals_share_one_pool(self):
        # review: original_for made and shut down a thread pool for every download on the request path
        from unittest import mock
        import subsync.worker as worker_module
        made, real = [], worker_module.ThreadPoolExecutor
        self.waiting()
        with mock.patch.object(worker_module, "ThreadPoolExecutor", lambda *a, **k: made.append(k) or real(*a, **k)):
            for _ in range(2):
                with self.worker.cv:
                    self.worker.fetched_subs.clear()
                code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
                self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertEqual(self.downloads.asked, ["http://opensubs/x.srt"] * 2)
        self.assertEqual(made, [])

    def served(self, path):
        """A route's answer: (status, what it says it served, the headers a web player may read)."""
        code, _, headers = self.get(path)
        return code, headers.get(SERVED), headers.get("Access-Control-Expose-Headers")

    def test_each_answer_of_the_pick_route_says_what_it_served(self):
        # An original is sent as text with 200, as a synced copy is; a header tells them apart, for
        # measurement and debugging, readable by a web player too; a redirect has none
        pick = "/pick/" + token(self.ALIAS)
        self.waiting()
        self.assertEqual(self.served(pick + "/eng.srt"), (200, "original", SERVED))
        self.assertEqual(self.served(pick + "/rus.srt"), (200, "note", SERVED))        # no Russian subtitle at all
        self.worker.provisional[self.ALIAS] = {"rus": "1\n00:00:01,000 --> 00:00:02,000\nanchored\n"}
        self.assertEqual(self.served(pick + "/rus.srt"), (200, "guess", SERVED))
        self.s.pick_wait = 0.3                                          # the original's download outlasts the pick
        release = threading.Event()
        self.addCleanup(release.set)
        self.downloads.hold["http://opensubs/x.srt"] = release
        with self.worker.cv:
            self.worker.fetched_subs.clear()
        self.assertEqual(self.served(pick + "/eng.srt"), (302, None, None))
        release.set()
        self.store.put("f" * 16 + "-5", SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n",
                       fmt="srt")
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        self.assertEqual(self.served(pick + "/eng.srt"), (200, "copy", SERVED))

    def test_each_answer_of_the_pending_route_says_what_it_served(self):
        pending = "/pending/" + token(self.ALIAS) + "/eng.srt"
        self.assertEqual(self.served(pending), (404, None, None))         # a file subsync knows nothing of
        self.waiting()
        self.assertEqual(self.served(pending), (200, "original", SERVED))
        self.downloads.files["http://opensubs/x.srt"] = dialogue_srt("spa").encode()   # its text isn't English
        with self.worker.cv:
            self.worker.fetched_subs.clear()
        self.assertEqual(self.served(pending), (200, "note", SERVED))
        key = self.store.put("f" * 16 + "-5", SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n",
                             fmt="srt", offer=False)
        self.worker._publish(self.ALIAS, "eng", key)
        self.assertEqual(self.served(pending), (200, "copy", SERVED))

    def test_an_unknown_link_starts_nothing(self):
        self.assertEqual(self.get("/pick/" + token("name:other.mp4|9") + "/eng.srt")[0], 404)
        self.assertEqual(self.get("/pick/!!!/eng.srt")[0], 404)
        self.assertEqual(len(self.worker.jobs), 0)

    def test_a_waiting_files_guess_made_from_an_ass_subtitle_is_sent_as_srt(self):
        # The anchored guess keeps its subtitle's format (subs.write_like), and an ASS guess (common for
        # anime) went out as ASS under the .srt link
        self.waiting()
        self.worker.provisional[self.ALIAS] = {"rus": (
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,anchored\n")}
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")
        self.assertEqual((code, body.decode(), headers.get(SERVED)),
                         (200, "1\n00:00:01,000 --> 00:00:02,000\nanchored\n", "guess"))

    def test_a_copy_ready_before_the_deadline_wins_over_the_original(self):
        # A pick waits for the copy until 3 s before its deadline, then spent those 3 s on the original and
        # never looked again: a copy published meanwhile lost to the original or its redirect (Game of Thrones S01E01
        # in the 1.6.0 T2: the copy ready 1.7 s before the deadline, the pick answered with the original's redirect).
        # The download now stops waiting once a copy is published, and the copy is looked for once more before an
        # original goes out; the pending entry too
        copy = "1\n00:00:01,000 --> 00:00:02,000\nsynced\n"
        for route in ("pick", "pending"):
            with self.subTest(route=route):
                self.s.pick_wait = 3.0
                self.waiting()
                with self.worker.cv:
                    self.worker.done.clear()
                    self.worker.fetched_subs.clear()
                release = threading.Event()
                self.addCleanup(release.set)
                self.downloads.hold["http://opensubs/x.srt"] = release     # the original's download outlasts the pick

                def published():
                    key = self.store.put("f" * 16 + "-5", SHA_A, "eng", 0, "synced", body=copy, fmt="srt", offer=False)
                    self.worker._publish(self.ALIAS, "eng", key)
                timer = threading.Timer(0.5, published)
                self.addCleanup(timer.cancel)
                timer.start()
                t0 = time.time()
                code, body, headers = self.get(f"/{route}/{token(self.ALIAS)}/eng.srt")
                self.assertEqual((code, body.decode(), headers.get(SERVED)), (200, copy, "copy"))
                self.assertLess(time.time() - t0, 2.0)                   # as it was published, not at the deadline

    def test_a_copy_stored_while_the_original_downloaded_is_served_instead(self):
        # The original arrived in time, but a copy came first: the copy is looked for once more before the
        # original goes out
        self.s.pick_wait = 3.0
        self.waiting()
        release = threading.Event()
        self.addCleanup(release.set)
        self.downloads.hold["http://opensubs/x.srt"] = release
        copy = "1\n00:00:01,000 --> 00:00:02,000\nsynced\n"

        def stored_then_downloaded():
            self.store.put("f" * 16 + "-5", SHA_A, "eng", 0, "synced", body=copy, fmt="srt")
            self.store.alias(self.ALIAS, "f" * 16 + "-5")
            release.set()
        timer = threading.Timer(0.3, stored_then_downloaded)
        self.addCleanup(timer.cancel)
        timer.start()
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode(), headers.get(SERVED)), (200, copy, "copy"))
        self.assertEqual(self.downloads.asked, ["http://opensubs/x.srt"])

    def test_a_head_answers_with_the_headers_only_and_starts_nothing(self):
        # A HEAD ran the whole GET: on a pick entry it started the audio route (60-350 MB read, one of the
        # day's audio syncs) and waited, on a subtitle list it queued the file, on an original it downloaded it. Now
        # the list and the entries answer 200 with their content type and do nothing; /health, /manifest.json, /file
        # and /notice.srt as before (test_a_head_is_answered_with_the_headers_only)
        self.waiting()
        srt, tok = "application/x-subrip; charset=utf-8", token(self.ALIAS)
        for path, ctype in ((self.URL, "application/json; charset=utf-8"), (f"/pick/{tok}/eng.srt", srt),
                            (f"/pending/{tok}/eng.srt", srt), (f"/original/{tok}/eng.srt", srt)):
            with self.subTest(path=path):
                data = exchange(self.port, f"HEAD {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
                head, _, rest = data.partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(b"HTTP/1.1 200 "), head)
                self.assertIn(f"\r\nContent-Type: {ctype}\r\n".encode(), head + b"\r\n")
                self.assertNotIn(b"Content-Length", head)               # a HEAD's length must be its GET's
                self.assertEqual(rest, b"")
        self.assertIn(self.ALIAS, self.worker.pickable)                 # not picked: no audio route started
        self.assertEqual((len(self.worker.jobs), self.app.subtitle_requests, self.downloads.asked), (0, 0, []))


class NoTrack:
    """A file's index with no subtitle track of its own: nothing to judge against but its speech."""

    def text_tracks(self):
        return []

    def image_tracks(self):
        return []


class OriginalEntryFixture(AddonCase):
    """A file with English, Spanish and Russian subtitles, each kept as its original by a stub judging that measures it
    as CANDS says, behind the addon's server over a worker that is not started. No tests of its own."""
    URL = "/subtitles/movie/tt9/filename=b.mp4&videoSize=5.json"
    ALIAS = "name:b.mp4|5"
    FILE = "f" * 16 + "-5"
    # each candidate's text, and how the stub judging measures it (its share of lines within 200 ms as it is)
    CANDS = {"e1": ("eng", 30.0, 0.30), "e2": ("eng", 31.0, 0.55), "s1": ("spa", 30.0, 0.20), "s2": ("spa", 31.0, 0.10),
             "r1": ("rus", 30.0, 0.40)}

    def setUp(self):
        watch_the_log(self)
        aios = FakeAios()
        aios.subtitles = lambda *a: [dict(c) for c in self.cands]
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_AUDIO_ROUTE": "on",
                         "SUBSYNC_LANGUAGES": "eng,spa,rus", "SUBSYNC_PICK_WAIT_MS": "2000",
                         "SUBSYNC_PARALLEL_LANGUAGES": "off"}, aios)
        self.texts = {cid: dialogue_srt(lang, start=start) for cid, (lang, start, _) in self.CANDS.items()}
        self.measures = {self.texts[cid]: m for cid, (_, _, m) in self.CANDS.items()}
        self.downloads = fake_downloads(self)
        self.downloads.files.update({f"http://subs.test/{cid}.srt": t.encode() for cid, t in self.texts.items()})
        self.cands = [{"id": cid, "url": f"http://subs.test/{cid}.srt", "lang": lang, "release": ""}
                      for cid, (lang, _, _) in self.CANDS.items()]
        self.serve_addon()
        self.job = Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
        from unittest import mock
        from subsync import pipeline

        def sync_subtitle(index, text, lang, **kw):   # every candidate keeps its original, measured as above
            return {"outcome": "keep_original", "reason": "below the bar or shift under 100 ms",
                    "route": "audio" if kw.get("audio") else "cues",
                    "gate_original": {"within200": self.measures.get(text), "median_ms": 300}}
        for name, fake in (("check_original", lambda *a, **k: None), ("shift_sync", lambda *a, **k: None),
                           ("sync_subtitle", sync_subtitle)):
            patcher = mock.patch.object(pipeline, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def get(self, url):
        """A link's answer as a player that doesn't follow redirects gets it: a link the app gave out, or one on its
        server."""
        return http(url.replace("http://subsync:8790", self.base), follow=False)

    def listed(self):
        return json.loads(http(self.base + self.URL)[1])["subtitles"]

    def read(self, reference):
        """The file's index was read just now (no new job), with or without a subtitle track of its own."""
        self.store.file_read(self.FILE, 5, "f" * 16, reference, {"rule": self.s.rule_id, "reference": reference})
        self.store.alias(self.ALIAS, self.FILE)

    def judge(self, job, langs):
        """The job judges each language's candidates (every one kept), then ends."""
        for lang in langs:
            self.worker._sync_language(job, NoTrack(), self.FILE, lang, [c for c in self.cands if c["lang"] == lang],
                                       None, "b.mp4")
        self.store.judged_put(self.FILE, self.s.languages, self.s.rule_id)
        with self.worker.cv:
            self.worker.jobs.clear()
            self.worker.queue.clear()

    def wait_for(self, check, timeout=3.0):
        t0 = time.time()
        while not check() and time.time() - t0 < timeout:
            time.sleep(0.02)
        return check()


class OriginalEntryTest(OriginalEntryFixture):
    """A language that has subtitles but no synced copy, on a file that neither waits for a pick nor has a job on
    it, is listed once, after the synced ones, as "<Language> (original)": the subtitle, unchanged, that the file's
    judging measured best as it is, else the likeliest by its name, and only one whose text is in that language.
    Downloading it serves that subtitle's text from this person's own list; it queues no job and reads nothing of the
    file."""

    def test_the_bears_shape(self):
        # three languages wait for a pick; the pick; the audio job judges every candidate "keep original": each language
        # keeps one entry, serving its best-measured original (The Bear's list went from 3 to pick to 0 entries)
        self.worker._candidates(self.job, dict(self.job.hints))
        self.worker._set_originals(self.ALIAS, self.cands, "b.mp4", None)
        with self.worker.cv:
            self.worker._put_waiting(self.job)
        self.read(False)
        self.assertEqual([e["label"] for e in self.listed()],
                         ["English (Subsync)", "Spanish (Subsync)", "Russian (Subsync)"])
        code, body, _ = self.get(f"http://subsync:8790/pick/{token(self.ALIAS)}/eng.srt")
        self.assertEqual((code, body.decode()), (200, self.texts["e1"]))     # the name-ranked guess, checked
        audio = next(iter(self.worker.jobs.values()))
        self.assertTrue(audio.audio)
        self.judge(audio, self.s.languages)
        listed = self.listed()
        self.assertEqual([(e["lang"], e["label"]) for e in listed],
                         [("eng", "English (original)"), ("spa", "Spanish (original)"), ("rus", "Russian (original)")])
        self.assertTrue(listed[0]["url"].endswith(f"/original/{token(self.ALIAS)}/eng.srt"))
        self.assertEqual([e["variantOf"] for e in listed], ["e2", "s1", "r1"])
        asked = len(self.downloads.asked)
        for entry, best in zip(listed, ("e2", "s1", "r1")):
            code, body, headers = self.get(entry["url"])
            self.assertEqual((code, body.decode()), (200, self.texts[best]))
            self.assertTrue(headers["Content-Type"].startswith("application/x-subrip"))
        # its download queues no job and reads nothing of the file: only the subtitles, and those kept a moment ago
        self.assertEqual((len(self.worker.jobs), len(self.worker.picked_early)), (0, 0))
        self.assertEqual(self.store.day_count("read"), 0)
        self.assertEqual(len(self.downloads.asked), asked)

    def test_friends_shape(self):
        # a Russian copy; English and Spanish judged against the file's own track and kept: Russian "(synced)", English
        # and Spanish "(original)", after it
        key = self.store.put(self.FILE, SHA_A, "rus", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nкопия\n",
                             fmt="srt", candidate_id="r1", offer=False)
        self.store.set_offers(self.FILE, "rus", [key])
        self.read(True)
        self.worker.remember(self.job)
        self.judge(Job("movie", "tt9", dict(self.job.hints)), ["eng", "spa"])
        self.assertEqual([(e["lang"], e["label"]) for e in self.listed()],
                         [("rus", "Russian (synced)"), ("eng", "English (original)"), ("spa", "Spanish (original)")])
        code, body, _ = self.get(f"http://subsync:8790/original/{token(self.ALIAS)}/spa.srt")
        self.assertEqual((code, body.decode()), (200, self.texts["s1"]))   # its list fetched again: none was kept

    def test_a_language_in_another_language_or_without_subtitles_gets_none(self):
        self.downloads.files["http://subs.test/r1.srt"] = self.texts["e1"].encode()   # "Russian", but English text
        self.cands = [c for c in self.cands if c["lang"] != "spa"]                     # no Spanish subtitle at all
        self.read(True)
        self.worker.remember(self.job)
        self.judge(Job("movie", "tt9", dict(self.job.hints)), self.s.languages)
        self.assertEqual([e["label"] for e in self.listed()], ["English (original)"])
        self.assertIsNone(self.store.kept_original(self.FILE, "rus")["candidate_id"])

    def test_a_file_not_judged_lists_the_likeliest_by_name_and_checks_it_when_served(self):
        # its job ended before judging (a read that failed after the list): the name-ranked guess
        self.downloads.files["http://subs.test/e1.srt"] = dialogue_srt("spa").encode()   # "English", but Spanish text
        self.worker._candidates(self.job, dict(self.job.hints))
        self.worker._set_originals(self.ALIAS, self.cands, "b.mp4", None)
        self.worker.remember(self.job)
        self.store.file_failed(self.ALIAS)
        self.worker.unlisted[self.ALIAS] = time.time()                                 # no new job meanwhile
        self.assertEqual([e["label"] for e in self.listed()],
                         ["English (original)", "Spanish (original)", "Russian (original)"])
        code, body, _ = self.get(f"http://subsync:8790/original/{token(self.ALIAS)}/eng.srt")
        self.assertEqual((code, body.decode()), (200, self.texts["e2"]))   # e1 refused when served: the next by name
        self.assertEqual(len(self.worker.jobs), 0)

    def test_the_original_route_says_what_it_served(self):
        # The "(original)" entry's subtitle sent unchanged, or the note when none of its language is in it
        self.downloads.files["http://subs.test/r1.srt"] = self.texts["e1"].encode()   # "Russian", but English text
        self.read(True)
        self.worker.remember(self.job)
        self.judge(Job("movie", "tt9", dict(self.job.hints)), self.s.languages)
        for lang, served in (("eng", "original"), ("rus", "note")):
            code, _, headers = self.get(f"http://subsync:8790/original/{token(self.ALIAS)}/{lang}.srt")
            self.assertEqual((code, headers.get(SERVED), headers.get("Access-Control-Expose-Headers")),
                             (200, served, SERVED))

    def test_an_unknown_link_starts_nothing(self):
        code, _, _ = self.get(f"http://subsync:8790/original/{token('name:other.mp4|9')}/eng.srt")
        self.assertEqual(code, 404)
        self.assertEqual(self.get("http://subsync:8790/original/!!!/eng.srt")[0], 404)
        self.assertEqual((len(self.worker.jobs), self.downloads.asked), (0, []))

    def test_a_list_asked_again_on_a_requests_time_answers_late_and_is_kept_for_the_next_try(self):
        # A person's list fetched again for an original keeps to the request's deadline: slower, the answer is
        # "late" (the player asks again) and the list, once it comes, is kept for that next try
        listed, slow = self.worker.aios.subtitles, threading.Event()

        def slow_list(*a):
            slow.wait(5)
            return listed(*a)
        self.worker.aios.subtitles = slow_list
        self.worker.remember(self.job)
        t0 = time.time()
        self.assertEqual(self.worker.original_for(self.ALIAS, "eng", time.time() + 0.3), (None, "late", None))
        self.assertLess(time.time() - t0, 1.5)
        slow.set()
        self.assertTrue(self.wait_for(lambda: self.ALIAS in self.worker.lists))
        self.worker.aios.subtitles = lambda *a: self.fail("the list kept is used")
        self.assertEqual(self.worker.original_for(self.ALIAS, "eng", time.time() + 3), (self.texts["e1"], None, None))

    def test_a_download_an_earlier_try_started_is_waited_for_not_started_again(self):
        # A player's next try while the download its first try started still runs (a slow site, a 429 retry)
        # waits for that download: the site is asked, and its limit counted, once
        self.worker._candidates(self.job, dict(self.job.hints))
        self.worker.remember(self.job)
        url = "http://subs.test/e1.srt"
        self.downloads.hold[url] = release = threading.Event()
        for _ in range(2):                           # the first try, then the player's next one after the 503
            self.assertEqual(self.worker.original_for(self.ALIAS, "eng", time.time() + 0.3)[1], "late")
        release.set()
        self.assertEqual(self.worker.original_for(self.ALIAS, "eng", time.time() + 3), (self.texts["e1"], None, None))
        self.assertEqual(self.downloads.asked.count(url), 1)
        self.assertTrue(self.wait_for(lambda: not self.worker.fetching))


class MeasuredPickTest(OriginalEntryFixture):
    """A pick that nothing passed for by its deadline is served the downloaded original that measured best as it
    is against the file's own track, when it beats the name-ranked guess's measure (3 Idiots and Sherlock got the guess
    at 14 s); its text, checked, from the file's own job's download."""

    def setUp(self):
        super().setUp()
        self.s.pick_wait = 0.3
        from unittest import mock
        from subsync import pipeline

        def check_original(index, text, lang, **kw):   # not in sync as it is, measured as CANDS says
            return {"in_sync": False, "route": "cues",
                    "gate_original": {"within200": self.measures.get(text), "median_ms": 300}}
        patcher = mock.patch.object(pipeline, "check_original", check_original)
        patcher.start()
        self.addCleanup(patcher.stop)

    def judging(self):
        """The file's job, queued while its text pass judges English: nothing passes."""
        self.worker._candidates(self.job, dict(self.job.hints))
        self.worker._set_originals(self.ALIAS, self.cands, "b.mp4", None)
        self.assertEqual(self.worker.known_originals(self.ALIAS)["eng"]["id"], "e1")    # the name-ranked guess, 30 %
        self.worker.submit(Job("movie", "tt9", dict(self.job.hints)))
        self.worker._sync_language(self.job, NoTrack(), self.FILE, "eng", [c for c in self.cands if c["lang"] == "eng"],
                                   None, "b.mp4")
        self.store.alias(self.ALIAS, self.FILE)

    def pick(self, lang="eng"):
        return self.get(f"{self.base}/pick/{token(self.ALIAS)}/{lang}.srt")

    def test_nothing_passed_by_the_deadline_serves_the_best_measured_original(self):
        self.judging()
        code, body, _ = self.pick()
        self.assertEqual((code, body.decode()), (200, self.texts["e2"]))           # 55 % within 200 ms as it is
        self.assertEqual(self.downloads.asked.count("http://subs.test/e2.srt"), 1)   # the job's download, no other

    def test_a_pass_in_time_is_served_as_before(self):
        self.judging()
        key = self.store.put(self.FILE, SHA_A, "eng", 1, "synced", fmt="srt", offer=False,
                             body="1\n00:00:01,000 --> 00:00:02,000\nthe pass\n")
        self.worker._publish(self.ALIAS, "eng", key)
        code, body, _ = self.pick()
        self.assertEqual((code, b"the pass" in body), (200, True))


class LanesTest(unittest.TestCase):
    """The audio route (about a minute per file) runs on its own worker, so it never holds another file's job."""

    def test_a_plain_job_finishes_while_an_audio_job_runs(self):
        with TempDir() as d:
            s = Settings({"SUBSYNC_DATA": d})
            release, order = threading.Event(), []

            class W(Worker):
                def process(self, job):
                    if job.audio:
                        release.wait(10)
                    order.append(job.alias)
            w = W(s, closed_after(self, Store(d)), FakeAios(), Safety())
            audio = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})
            audio.audio = True
            w.submit(audio)
            w.start()
            # Both lanes stopped as the test ends, as it passes or not, the audio job one holds let go first
            self.addCleanup(w.stop)
            self.addCleanup(release.set)
            time.sleep(0.3)                                    # the audio job is running
            w.submit(Job("movie", "tt2", {"filename": "b.mkv", "videoSize": 2}))
            t0 = time.time()
            while "name:b.mkv|2" not in order and time.time() - t0 < 3:
                time.sleep(0.05)
            self.assertEqual(order, ["name:b.mkv|2"])           # done while the audio job still runs
            self.assertTrue(w.busy_with("name:a.mkv|1"))
            release.set()
            t0 = time.time()
            while len(order) < 2 and time.time() - t0 < 3:
                time.sleep(0.05)
            self.assertEqual(order, ["name:b.mkv|2", "name:a.mkv|1"])

    def test_a_lane_goes_on_when_the_store_fails_as_a_job_ends(self):
        # A store write that fails while a job's end is handled (a refused file's mark) or in the hourly
        # expiry (a full disk, a locked database) is logged; the lane runs the next job
        import sqlite3
        from subsync.reader import Refused
        with TempDir() as d:
            store, ran = closed_after(self, Store(d)), []

            def full(*a, **k):
                raise sqlite3.OperationalError("database or disk is full")
            store.expire = store.file_failed = full

            class W(Worker):
                def process(self, job):
                    ran.append(job.alias)
                    if job.video_id == "tt1":
                        raise Refused("HTTP 429")
            w = W(Settings({"SUBSYNC_DATA": d}), store, FakeAios(), Safety())
            w.submit(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1}))
            w.submit(Job("movie", "tt2", {"filename": "b.mkv", "videoSize": 2}))
            with self.assertLogs("subsync.worker", "WARNING") as logs:
                w.start()
                t0 = time.time()
                while len(ran) < 2 and time.time() - t0 < 3:
                    time.sleep(0.05)
                w.stop()
            self.assertEqual(ran, ["name:a.mkv|1", "name:b.mkv|2"])
            self.assertFalse(w.busy_with("name:a.mkv|1"))
            self.assertTrue(any("store expiry failed: OperationalError" in line for line in logs.output))

    def test_a_public_worker_leaves_the_store_expiry_to_the_housekeeping(self):
        # In public mode the shared store is expired by the housekeeping thread (__main__.housekeeping_round)
        # alone, not again by each person's worker after its first job; self-hosted, the worker expires its own store
        for public, expiries in ((True, 0), (False, 1)):
            with self.subTest(public=public), TempDir() as d:
                store, ran, expired = closed_after(self, Store(d)), [], []
                store.expire = lambda: expired.append(1)

                class W(Worker):
                    def process(self, job):
                        ran.append(job.alias)
                env = dict({"SUBSYNC_DATA": d}, **({"SUBSYNC_PUBLIC": "on"} if public else {}))
                w = W(Settings(env), store, FakeAios(), Safety())
                w.submit(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1}))
                w.start()
                t0 = time.time()
                while not ran and time.time() - t0 < 3:
                    time.sleep(0.05)
                w.stop()
                w.join(3)
                self.assertFalse(w.is_alive())
                self.assertEqual((ran, len(expired)), (["name:a.mkv|1"], expiries))


class StopTest(unittest.TestCase):
    """The self-hosted service's SIGTERM (docker stop) stops its worker as Worker.stop does, as public mode's
    does: its idle lanes end at once, and a pick queued but not started yet waits for another pick."""

    def test_sigterm_stops_the_worker_through_its_stop(self):
        import signal
        from unittest import mock
        from subsync import __main__ as entry
        pick = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})
        pick.audio, pick.not_before = True, time.time() + 600     # queued, not started before the stop
        handlers, servers = {}, []

        class Server:
            def __init__(self, app, port, host):
                self.app, self.host = app, host
                servers.append(self)

            def serve_forever(self):
                time.sleep(0.3)                     # both lanes wait for a job
                self.app.worker.submit(pick)
                handlers[signal.SIGTERM]()          # docker stop

            def shutdown(self):
                pass

        with TempDir() as d:
            env = {"SUBSYNC_DATA": d, "SUBSYNC_SOURCE_MANIFEST": "http://127.0.0.1:9/stremio/u1/E1/manifest.json",
                   "SUBSYNC_SOURCE_PASSWORD": "p1", "SUBSYNC_PUBLIC_BASE": "http://127.0.0.1:9"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("logging.basicConfig"), \
                    mock.patch("signal.signal", lambda sig, handler: handlers.__setitem__(sig, handler)), \
                    mock.patch.object(entry, "serve", Server), local_network_only(), \
                    self.assertLogs("subsync", "INFO"):
                entry.main()
            self.assertEqual(servers[0].host, "0.0.0.0")          # SUBSYNC_BIND's default
            worker = servers[0].app.worker
            self.assertIn(pick.alias, worker.pickable)
            for lane in (worker, worker.audio_worker):
                lane.join(2)
                self.assertFalse(lane.is_alive())


class EntryCodeTest(AddonFixture):
    """With SUBSYNC_ADDON_KEY set, each entry's address carries a code made from the key, so the entries stay
    open to players (their lists name them without the key) but an entry's address can't be made up: one without its
    code answers "not found" and starts nothing (it started audio syncs, picks and list fetches for anyone)."""
    URL = "/k3y/subtitles/movie/tt9/filename=b.mp4&videoSize=5.json"
    ALIAS = "name:b.mp4|5"

    def setUp(self):
        super().setUp()
        self.s.addon_key, self.s.audio_route = "k3y", True
        self.worker.pickable[self.ALIAS] = Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.store.file_read("f" * 16 + "-5", 5, "f" * 16, False, {})   # read a moment ago: no new job
        self.store.alias(self.ALIAS, "f" * 16 + "-5")

    def test_a_token_carries_the_code_of_its_alias_under_the_key(self):
        from subsync.addon import CODE_CHARS, alias_of, entry_code
        coded = token(self.ALIAS, "k3y")
        self.assertEqual(coded, token(self.ALIAS) + entry_code("k3y", self.ALIAS))
        self.assertEqual(len(entry_code("k3y", self.ALIAS)), CODE_CHARS)
        self.assertRegex(coded, r"^[A-Za-z0-9_-]+$")                       # what the routes take, unchanged
        self.assertNotIn("k3y", coded)
        self.assertEqual(alias_of(coded, "k3y"), self.ALIAS)
        self.assertEqual(alias_of(token(self.ALIAS)), self.ALIAS)          # no key: every route open, as before
        for forged in (token(self.ALIAS), token(self.ALIAS, "other"), token("name:c.mkv|7") + coded[-CODE_CHARS:],
                       coded[:-1] + ("A" if coded[-1] != "A" else "B"), "x", ""):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                alias_of(forged, "k3y")

    def test_the_lists_entries_carry_the_code_and_answer_without_the_key(self):
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertEqual([s["url"] for s in subs], [f"http://subsync:8790/pick/{token(self.ALIAS, 'k3y')}/eng.srt"])
        code, body, _ = self.get(f"/pick/{token(self.ALIAS, 'k3y')}/eng.srt")   # as a player fetches it, no key
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])

    def test_an_address_without_its_code_is_not_found_and_starts_nothing(self):
        calls = []
        for name in ("pick", "ask_list_again", "original_for", "prioritize"):
            patch = mock.patch.object(self.worker, name, lambda *a, name=name: calls.append(name))
            patch.start()
            self.addCleanup(patch.stop)
        unknown = "name:Film.2024.1080p.WEB.mkv|4521234567"            # a release the owner may open later
        for route in ("pick", "pending", "original"):
            for tok in (token(self.ALIAS), token(unknown), token(self.ALIAS, "other")):
                with self.subTest(route=route, tok=tok):
                    for prefix in ("", "/k3y"):
                        self.assertEqual(self.get(f"{prefix}/{route}/{tok}/eng.srt")[0], 404)
        self.assertEqual(calls, [])
        self.assertIn(self.ALIAS, self.worker.pickable)                  # still waiting for the owner's pick
        self.assertEqual((dict(self.worker.jobs), dict(self.worker.picked_early)), ({}, {}))

    def test_the_texts_name_the_entries_as_open(self):
        rule = dict(explain.RULES)["Links and keys stay private"]
        self.assertIn("The routes that make subsync work (its subtitle lists and status) answer only under a private key "
                      "in their address, and each entry in its lists carries a code made from that key, so nobody who "
                      "finds subsync's address can make it read files; only the synced files, those entries and a health "
                      "check are open.", rule)
        self.assertIn('Without the key those routes answer "not found", so nobody who finds subsync\'s address can make '
                      "it read files; synced files, /health and the entries of subsync's own lists stay open for your "
                      "apps, and an entry's address can't be made up without the key.",
                      explain.SETTINGS["SUBSYNC_ADDON_KEY"][1])
        self.assertTrue(explain.QUESTIONS["PUBLIC_ADDRESS"][1].endswith(
            "Only synced files, the entries subsync lists and a health check are open at that address."))


class AddonKeyTest(AddonCase):
    """The routes that start work or show status answer only under the private key; files and /health don't
    need it; a base path a reverse proxy forwards as is is removed first."""

    def setUp(self):
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "https://example.org/subsync", "SUBSYNC_ADDON_KEY": "/k3y/",
                         "SUBSYNC_BASE_PATH": "subsync/"})
        self.serve_addon()

    def test_settings_normalised(self):
        self.assertEqual((self.s.addon_key, self.s.base_path), ("k3y", "/subsync"))
        self.assertEqual((Settings({}).addon_key, Settings({}).base_path), ("", ""))

    def test_work_routes_need_the_key(self):
        for path in ("/manifest.json", "/status", "/subtitles/movie/tt1/filename=a.mkv&videoSize=5.json",
                     "/subsync/manifest.json", "/wrong/manifest.json", "/k3yy/manifest.json"):
            self.assertEqual(http(self.base + path)[0], 404, path)
        self.assertEqual(len(self.worker.jobs), 0)
        self.assertEqual(self.app.subtitle_requests, 0)
        for prefix in ("/k3y", "/subsync/k3y"):
            code, body, _ = http(self.base + prefix + "/manifest.json")
            self.assertEqual((code, json.loads(body)["resources"]), (200, ["subtitles"]))
        code, body, _ = http(self.base + "/k3y/subtitles/movie/tt1.json")
        self.assertEqual((code, json.loads(body)["subtitles"]), (200, []))
        code, body, _ = http(self.base + "/subsync/k3y/status")
        status = json.loads(body)
        self.assertEqual((code, status["subtitle_requests"]), (200, 1))
        self.assertIsInstance(status["last_subtitle_request"], int)
        self.assertEqual((status["busy"], status["busy_audio"]), (False, False))
        self.worker.current_audio = Job("movie", "tt1", {"filename": "a.mkv"})   # the audio worker on a file
        status = json.loads(http(self.base + "/subsync/k3y/status")[1])
        self.assertEqual((status["busy"], status["busy_audio"]), (True, True))

    def test_files_and_health_are_open(self):
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})
        code, body, _ = http(self.base + "/k3y/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        url = json.loads(body)["subtitles"][0]["url"]
        self.assertTrue(url.startswith(f"https://example.org/subsync/file/{ALGO_VERSION}_"))
        path = url[len("https://example.org"):]
        for p in (path, path[len("/subsync"):], "/k3y" + path[len("/subsync"):]):
            self.assertEqual(http(self.base + p)[0], 200, p)
        for p in ("/health", "/subsync/health", "/k3y/health"):
            self.assertEqual(http(self.base + p)[:2], (200, b"ok"), p)

    def test_the_images_health_check_asks_the_port_the_service_listens_on(self):
        # SUBSYNC_PORT moves the service (./setup sets it with host networking when 8790 is taken); the image's
        # HEALTHCHECK, run as Docker runs it (sh -c, in the container's environment), must ask that port
        import re
        import subprocess
        import tempfile
        from http.server import BaseHTTPRequestHandler, HTTPServer
        dockerfile = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Dockerfile")
        if not os.path.exists(dockerfile):
            self.skipTest("the Dockerfile is in the source, not in the image")
        with open(dockerfile) as f:
            command = re.search(r"\nHEALTHCHECK [^\n]*\\\n\s*CMD (.+)\n", f.read()).group(1)

        class Unwell(BaseHTTPRequestHandler):      # another program, on another port, that isn't well
            def do_GET(self):
                self.send_response(503)
                self.end_headers()

            def log_message(self, *a):
                pass
        other = start_server(HTTPServer(("127.0.0.1", 0), Unwell))
        self.addCleanup(stop_server, other)
        with tempfile.TemporaryDirectory() as bin_:
            python = os.path.join(bin_, "python")
            with open(python, "w") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            os.chmod(python, 0o755)

            def check(port):
                env = {"PATH": f"{bin_}:/usr/bin:/bin", "SUBSYNC_PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"}
                return subprocess.run(["sh", "-c", command], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                      timeout=30).returncode
            self.assertEqual(check(self.server.server_address[1]), 0)
            self.assertNotEqual(check(other.server_address[1]), 0)

    def test_a_key_is_compared_as_the_address_carries_it_decoded(self):
        # A hand-set key with a space or an accent arrives percent-encoded and never matched: every keyed route
        # answered "not found" for good (a key with a slash stops the start: test_settings)
        for key, segment in (("my key", "my%20key"), ("clé", "cl%C3%A9"), ("k3y", "k3y"), ("k3y", "k%33y")):
            with self.subTest(key=key, segment=segment):
                app = App(Settings({"SUBSYNC_DATA": self.s.data, "SUBSYNC_ADDON_KEY": key}), self.store, self.worker,
                          Safety())
                self.assertEqual(app.route(f"/{segment}/manifest.json"), ("/manifest.json", True))
                self.assertEqual(app.route("/my%20kez/manifest.json"), ("/my%20kez/manifest.json", False))
        code, body, _ = http(self.base + "/%6B3y/manifest.json")              # over HTTP too
        self.assertEqual((code, json.loads(body)["resources"]), (200, ["subtitles"]))

    def test_no_key_keeps_the_old_routes(self):
        app = App(Settings({"SUBSYNC_DATA": self.s.data}), self.store, self.worker, Safety())
        self.assertEqual(app.route("/manifest.json"), ("/manifest.json", True))
        self.assertEqual(app.route("/k3y/manifest.json"), ("/k3y/manifest.json", True))


class NameOnlyTest(AddonCase):
    """A request with a file name but no size names that file, never just the title."""

    def setUp(self):
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790"})

    def test_alias(self):
        self.assertEqual(Job("movie", "tt1", {"filename": "a.mkv"}).alias, "name:a.mkv|None")
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab", "videoSize": 3}).alias, "hash:ab|3")
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab"}).alias, "hash:ab|None")
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab", "videoSize": 0}).alias, "hash:ab|None")
        self.assertEqual(Job("movie", "tt1").alias, "title:movie/tt1")

    def test_aliases(self):
        # The aliases the store may know a request's file by, in one place (written out four times before):
        # its alias, and with a file name its name and size
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab", "videoSize": 3, "filename": "a.mkv"}).aliases,
                         ("hash:ab|3", "name:a.mkv|3"))
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab", "filename": "a.mkv"}).aliases, ("name:a.mkv|None",))
        self.assertEqual(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 3}).aliases, ("name:a.mkv|3",))
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab", "videoSize": 3}).aliases, ("hash:ab|3",))
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab"}).aliases, ("hash:ab|None",))
        self.assertEqual(Job("movie", "tt1").aliases, ("title:movie/tt1",))

    def test_another_release_of_the_title_gets_nothing_of_this_file(self):
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        self.store.alias(Job("movie", "tt1", {"filename": "a.mkv"}).alias, FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})
        self.assertEqual(len(self.app.subtitles("movie", "tt1", "filename=a.mkv")), 1)
        other = self.app.subtitles("movie", "tt1", "filename=b.mkv")
        # b.mkv is queued for its own sync: only its own "(Subsync)" entries, nothing made for a.mkv
        self.assertEqual([s["label"] for s in other], ["English (Subsync)"])
        self.assertTrue(other[0]["url"].endswith("/pick/" + token("name:b.mkv|None") + "/eng.srt"))
        self.assertEqual(self.app.subtitles("movie", "tt1", ""), [])

    def test_a_hash_without_a_size_names_its_own_file(self):
        # A request with a file's hash but no size was filed under the title alone, and the file read for it
        # linked to the title: the next such request for another release of the title, and any request naming no
        # file, listed that file's copies, timed for another release, and its own file was never read
        own = "00112233aabbccdd-1000"     # its file's key starts with its hash (a hash leads to no other file)
        self.store.put(own, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        for alias in Job("movie", "tt1", {"videoHash": "00112233aabbccdd"}).aliases:   # as the worker links its file
            self.store.alias(alias, own)
        self.store.file_read(own, 1000, own[:16], True, {})
        self.assertEqual([s["label"] for s in self.app.subtitles("movie", "tt1", "videoHash=00112233aabbccdd")],
                         ["English (synced)"])
        other = self.app.subtitles("movie", "tt1", "videoHash=ffffeeee11112222")
        self.assertEqual([s["label"] for s in other], ["English (Subsync)"])     # queued for its own read
        self.assertTrue(other[0]["url"].endswith("/pick/" + token("hash:ffffeeee11112222|None") + "/eng.srt"))
        self.assertEqual(list(self.worker.jobs), ["hash:ffffeeee11112222|None"])
        self.assertEqual(self.app.subtitles("movie", "tt1", ""), [])


class ReadLatelyTest(AddonCase):
    """Whether a file was read lately is asked of the store, on its own clock (the addon read store.db itself,
    outside the store's lock, and compared with time.time())."""

    def setUp(self):
        self.clock = Clock()
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790"}, clock=self.clock)

    def test_a_file_read_a_day_ago_on_the_stores_clock_is_read_again(self):
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})
        self.clock.t += REPROCESS_S - 1
        self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(list(self.worker.jobs), [])                            # read lately: nothing queued
        self.clock.t += 2
        self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(list(self.worker.jobs), ["name:a.mkv|1000"])


class ConnectionTest(AddonCase):
    """The self-hosted service's HTTP: what a request may carry and what its connection does."""

    def setUp(self):
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790"})
        self.serve_addon()

    def queued(self):
        """The hints of the jobs queued since the last call."""
        with self.worker.cv:
            jobs = list(self.worker.jobs.values())
            self.worker.jobs.clear()
            self.worker.queue.clear()
        return [j.hints for j in jobs]

    def test_a_body_no_route_reads_closes_the_connection(self):
        # The body stayed on the kept-open connection and was read as the next request, so a request hidden in
        # it was answered too (behind a web server that keeps its connections, to whoever it sent next)
        hidden = b"GET /status HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        length = b"Content-Length: %d" % len(hidden)
        for head in (length, length + b"\r\nTransfer-Encoding: chunked"):
            with self.subTest(head=head):
                data = exchange(self.port, b"GET /health HTTP/1.1\r\nHost: x\r\n" + head + b"\r\n\r\n" + hidden)
                self.assertEqual(answers(data), [200])
                self.assertIn(b"\r\nConnection: close\r\n", data)
                self.assertNotIn(b'"store"', data)
        chunked = b"%x\r\n" % len(hidden) + hidden + b"\r\n0\r\n\r\n"
        data = exchange(self.port, b"GET /health HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + chunked)
        self.assertEqual(answers(data), [200])

    def test_a_request_without_a_body_keeps_the_connection(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        for headers in ({}, {"Content-Length": "0"}):
            conn.request("GET", "/health", headers=headers)
            r = conn.getresponse()
            self.assertEqual((r.status, r.read(), r.getheader("Connection")), (200, b"ok", None))
            sock = conn.sock
            conn.request("GET", "/manifest.json")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
            self.assertIs(conn.sock, sock)                         # the same connection answered both

    def test_an_idle_kept_open_connection_is_closed(self):
        # It held its thread for good (public mode closed it after 30 s)
        handler = self.server.RequestHandlerClass
        self.assertIsNotNone(handler.timeout)
        self.assertLessEqual(handler.timeout, 30)
        with mock.patch.object(handler, "timeout", 0.3), socket.create_connection(("127.0.0.1", self.port), 5) as sock:
            t0 = time.time()
            self.assertEqual(sock.recv(1), b"")
            self.assertLess(time.time() - t0, 3)

    def test_each_server_answers_with_its_own_app(self):
        # Serve set the app on Handler itself, so the last server made answered for every one
        other = App(self.s, self.store, self.worker, self.safety)
        other.subtitle_requests = 7
        self.addCleanup(serve(other, 0).server_close)
        code, body, _ = http(self.base + "/status")
        self.assertEqual((code, json.loads(body)["subtitle_requests"]), (200, 0))

    def test_answers_name_no_python_version(self):
        # Every answer said "BaseHTTP/0.6 Python/3.x.y"
        for method in ("GET", "POST"):
            _, _, headers = http(self.base + "/health", method=method, data=b"" if method == "POST" else None)
            self.assertEqual(headers["Server"].strip(), "AIOSubtitles")

    def test_an_id_no_title_has_is_404_and_never_logged(self):
        # The id went to the log as it was: a line break in it wrote a log line of the sender's own
        forged = "tt1%0A2026-11-17%2000:00:00,000%20ERROR%20subsync.worker%20forged"
        with self.assertNoLogs("subsync", level="INFO"):
            for vid in (forged, "tt1%09x", "tt1%C2%85x", "tt" + "1" * 300):
                code, body, _ = http(self.base + f"/subtitles/movie/{vid}/filename=q.mkv&videoSize=3.json")
                self.assertEqual((code, body), (404, b""))
        self.assertEqual((self.queued(), self.app.subtitle_requests), ([], 0))
        for i, vid in enumerate(("kitsu:46474:1", "tt2%3A1%3A1", "tt" + "1" * 200)):
            code, _, _ = http(self.base + f"/subtitles/series/{vid}/filename=q{i}.mkv&videoSize=3.json")
            self.assertEqual(code, 200)
        self.assertEqual(len(self.queued()), 3)

    def test_an_id_with_a_slash_is_404(self):
        # "%2F" became "/" in the id, which went on into AIOStreams' address as a path; no title's id has one
        with self.assertNoLogs("subsync", level="INFO"):
            for vid in ("tt5%2F..%2F..%2F..%2F..%2Fapi%2Fv1%2Fuser", "tt1%2F2"):
                code, body, _ = http(self.base + f"/subtitles/movie/{vid}/filename=q.mkv&videoSize=3.json")
                self.assertEqual((code, body), (404, b""))
        self.assertEqual((self.queued(), self.app.subtitle_requests), ([], 0))

    def test_a_size_in_other_digits_is_left_out_not_a_failure(self):
        # "videoSize=²" answered 500
        code, _, _ = http(self.base + "/subtitles/movie/tt1/filename=a.mkv&videoSize=%C2%B2.json")
        self.assertEqual(code, 200)
        self.assertEqual(self.queued(), [{"filename": "a.mkv"}])

    def test_extras_are_split_before_each_value_is_decoded(self):
        # The whole segment was decoded before its split: "Tom%20%26%20Jerry.mkv" arrived as "Tom ", a file
        # never found in the listing
        stremio = urllib.parse.quote("filename=Tom+%26+Jerry.mkv&videoSize=5", safe="")   # the segment encoded again
        for segment, name in (("videoSize=5&filename=Tom%20%26%20Jerry.mkv", "Tom & Jerry.mkv"),
                              ("filename=Law.%26.Order.S01E01.mkv&videoSize=5", "Law.&.Order.S01E01.mkv"),
                              ("filename=100%25AB.mkv&videoSize=5", "100%AB.mkv"),
                              ("filename=Tom+%26+Jerry.mkv&videoSize=5", "Tom & Jerry.mkv"),
                              (stremio, "Tom & Jerry.mkv")):
            with self.subTest(segment=segment):
                self.assertEqual(http(self.base + f"/subtitles/movie/tt1/{segment}.json")[0], 200)
                self.assertEqual(self.queued(), [{"filename": name, "videoSize": 5}])


class FetchSubtitleTest(unittest.TestCase):
    """sources.fetch_subtitle: what is unpacked from a download is bounded as the download is, and a
    "too many requests" answer is let go before the wait."""
    MAX = sources.MAX_SUBTITLE_BYTES
    TEXT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    BOMB = 32 * 1024 * 1024          # unpacked; a few kilobytes packed

    def fetch(self, body, url="https://subs.test/a.srt"):
        with mock.patch.object(sources.realdebrid, "urlopen", lambda req, timeout: io.BytesIO(body)):
            return fetch_subtitle(url)

    def peak(self, body):
        """(what fetching `body` gave, in short: its size or the error; the most memory Python held meanwhile)."""
        tracemalloc.start()
        try:
            try:
                got = f"{len(self.fetch(body))} bytes"
            except Exception as e:
                got = f"{type(e).__name__}: {e}"
            return got, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    @staticmethod
    def zipped(data, method, declared=None):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", method) as z:
            z.writestr("a.srt", data)
            if declared is not None:   # the central directory's size of it: whoever made the zip writes that
                z.filelist[-1].file_size = declared
        return out.getvalue()

    def test_gzip_is_unpacked_within_the_size_limit(self):
        # The download was bounded, its gunzip wasn't: a few kilobytes came out at hundreds of megabytes
        self.assertEqual(self.fetch(gzip.compress(self.TEXT[:20]) + gzip.compress(self.TEXT[20:])), self.TEXT)
        self.assertEqual(len(self.fetch(gzip.compress(b"x" * self.MAX))), self.MAX)
        got, peak = self.peak(gzip.compress(bytes(self.BOMB)))
        self.assertEqual(got, "ValueError: subtitle too large")
        self.assertLess(peak, 3 * self.MAX)

    def test_a_zip_member_is_unpacked_within_the_size_limit(self):
        # A member was unpacked whole by one read (deflate up to 1 GiB, bzip2 and LZMA without any bound)
        # before the size the zip declares for it was checked
        for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
            self.assertEqual(self.fetch(self.zipped(self.TEXT, method)), self.TEXT)
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):   # no subtitle site's, and unpacked without a bound
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "no subtitle in the zip"):
                self.fetch(self.zipped(self.TEXT, method))
        # members declaring 100 bytes that unpack to BOMB: deflate's is read to its declared size (its check sum then
        # fails), bzip2's isn't read
        for method, outcome in ((zipfile.ZIP_DEFLATED, r"^BadZipFile: Bad CRC-32"),
                                (zipfile.ZIP_BZIP2, r"^ValueError: no subtitle in the zip$")):
            with self.subTest(bomb=method):
                got, peak = self.peak(self.zipped(bytes(self.BOMB), method, declared=100))
                self.assertRegex(got, outcome)
                self.assertLess(peak, 3 * self.MAX)

    def test_a_429_is_let_go_before_the_wait(self):
        # Its connection was held through the wait
        refusal = urllib.error.HTTPError("https://dl.opensubtitles.test/a.srt", 429, "Too Many Requests",
                                         {"Retry-After": "2"}, io.BytesIO(b"slow down"))
        answers = [refusal, io.BytesIO(self.TEXT)]

        def urlopen(req, timeout):
            got = answers.pop(0)
            if isinstance(got, Exception):
                raise got
            return got
        waits = []

        def sleep(seconds):   # (the wait, whether the 429 was let go by then)
            waits.append((seconds, refusal.fp.closed))
        with mock.patch.object(sources.realdebrid, "urlopen", urlopen), \
                mock.patch.object(sources, "time", types.SimpleNamespace(sleep=sleep)):
            self.assertEqual(fetch_subtitle("https://dl.opensubtitles.test/a.srt"), self.TEXT)
        self.assertEqual(waits, [(2.0, True)])

    def test_a_retry_after_that_is_no_number_waits_the_step(self):
        # "nan" was waited as it was: time.sleep refused it, and the download failed
        for asked, wait in (("nan", sources.RETRY_STEP_S), ("Wed, 21 Oct 2026 07:28:00 GMT", sources.RETRY_STEP_S),
                            ("inf", sources.RETRY_WAIT_MAX_S), ("0", sources.RETRY_STEP_S)):
            with self.subTest(asked=asked):
                answers = [urllib.error.HTTPError("https://subs.test/a.srt", 429, "Too Many Requests",
                                                  {"Retry-After": asked}, io.BytesIO(b"")), io.BytesIO(self.TEXT)]

                def urlopen(req, timeout):
                    got = answers.pop(0)
                    if isinstance(got, Exception):
                        raise got
                    return got
                waits = []
                with mock.patch.object(sources.realdebrid, "urlopen", urlopen), \
                        mock.patch.object(sources, "time", types.SimpleNamespace(sleep=waits.append)):
                    self.assertEqual(fetch_subtitle("https://subs.test/a.srt"), self.TEXT)
                self.assertEqual(waits, [wait])


class PlayerAddressTest(unittest.TestCase):
    def test_only_an_address_that_can_go_out_as_it_is(self):
        # The redirect's Location is the address in this person's list as it is
        from subsync.worker import plain_address
        for url in ("http://opensubs/x.srt", "https://dl.example/a%20b.srt?x=1&y=2", "HTTPS://Host:8080/x"):
            self.assertTrue(plain_address(url), url)
        for url in ("", "http://opensubs/x.srt\r\nSet-Cookie: a=1", "http://opensubs/a b.srt", "http://opensubs/Ж.srt",
                    "http://opensubs/é.srt", "http://opensubs/\x7f", "ftp://opensubs/x.srt", "javascript:alert(1)",
                    "http:///x.srt", "http://[::1/x.srt", "//opensubs/x.srt"):
            self.assertFalse(plain_address(url), url)


class StreamDataTest(unittest.TestCase):
    def test_listing_user_agent_gets_stream_data_from_stock_aios(self):
        # Stock AIOStreams sends streamData only to clients whose User-Agent starts with "AIO": the listings and the
        # subtitle lists are asked with it (only the constant was checked, not what the requests send)
        self.assertTrue(sources.USER_AGENT.startswith("AIO"))
        asked = []

        class Listings(BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append((self.path.split("/")[4], self.headers.get("User-Agent")))
                body = b'{"streams": [], "subtitles": []}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), Listings))
        self.addCleanup(stop_server, server)
        aios = Aios(f"http://127.0.0.1:{server.server_address[1]}/stremio/u1/E1/manifest.json")
        aios.check_config = lambda: None
        self.assertEqual((aios.streams("movie", "tt1"), aios.subtitles("movie", "tt1", {"videoSize": 3})), ([], []))
        self.assertEqual(asked, [("stream", sources.USER_AGENT), ("subtitles", sources.USER_AGENT)])


class VersionTest(unittest.TestCase):
    """The version was written by hand in ten places, mostly wrong for 1.6.0 (the manifests' 1.0.0, User-Agents
    with 1.0, 1.6 or none): one module reads the release's VERSION once, and both manifests and every User-Agent take it
    from there, each User-Agent starting with "AIO" and naming the project's address."""

    def release(self):
        from helpers import SOURCE
        with open(os.path.join(os.path.dirname(SOURCE), "VERSION"), encoding="utf-8") as f:
            return f.read().strip()

    def test_the_manifest_and_every_user_agent_carry_the_releases_version(self):
        from subsync import aiosconfig, extra, origlang, reader, signup, version
        from subsync.addon import manifest
        release = self.release()
        self.assertRegex(release, r"^\d+\.\d+\.\d+$")
        self.assertEqual(version.VERSION, release)
        self.assertEqual(manifest(Settings({}))["version"], release)
        agent = f"AIOSubtitles/{release} ({{}}; +https://github.com/haslanli/AIOSubtitles)"
        self.assertEqual({sources.USER_AGENT, extra.USER_AGENT, origlang.USER_AGENT, reader.USER_AGENT},
                         {agent.format("subsync")})                     # the file reads too ("subsync/1" before)
        self.assertEqual((signup.USER_AGENT, aiosconfig.UA), (agent.format("public"), agent.format("setup")))

    def test_no_other_module_writes_a_version_or_a_user_agent_of_its_own(self):
        from subsync import version
        code = os.path.dirname(os.path.abspath(version.__file__))
        for name in sorted(os.listdir(code)):
            if name.endswith(".py") and name != "version.py":
                with self.subTest(module=name), open(os.path.join(code, name), encoding="utf-8") as f:
                    self.assertNotRegex(f.read(), r"AIOSubtitles/\d|subsync/\d|[\"']version[\"']: [\"']\d")


class AudioOffEntryTest(AddonCase):
    """With the audio option off, subsync answers at once; while it works on a file it lists one
    "<Language> (Subsync)" entry per language, whose download serves the synced copy when ready, else the original."""
    URL = "/subtitles/movie/tt9/filename=b.mkv&videoSize=5.json"
    ALIAS = "name:b.mkv|5"

    def setUp(self):
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_LANGUAGES": "eng,rus",
                         "SUBSYNC_PENDING_WAIT_MS": "3000", "SUBSYNC_PICK_WAIT_MS": "3000"})
        self.assertFalse(self.s.audio_route or self.s.pending_entry)    # the stock defaults
        fake_downloads(self)
        self.serve_addon()

    def test_the_first_answer_comes_at_once_with_an_entry_per_language(self):
        t0 = time.time()
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertLess(time.time() - t0, 1.0)                 # AIOStreams orders addons by who answers first
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs],
                         [("eng", "English (Subsync)", "original"), ("rus", "Russian (Subsync)", "original")])
        self.assertTrue(subs[0]["url"].endswith("/pick/" + token(self.ALIAS) + "/eng.srt"))
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, False)])

    def test_picking_the_entry_serves_the_copy_the_running_job_makes(self):
        http(self.base + self.URL)                                       # the job is queued
        with self.worker.cv:
            self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
            # the job downloaded the original first: the 3 s pick wait isn't shortened for its download
            self.worker.fetched_subs["http://opensubs/x.srt"] = (time.time(), X_TEXT.encode())

        def copy_ready():
            time.sleep(0.4)
            key = self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
            self.store.alias(self.ALIAS, FILE)
            self.worker._publish(self.ALIAS, "eng", key, final=True)     # the job's result for the language
        threading.Thread(target=copy_ready, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertEqual(body.decode(), "1\n00:00:01,000 --> 00:00:02,000\nHi\n")   # the copy, not the original
        self.assertLess(time.time() - t0, 2.5)

    def test_picking_after_the_original_was_kept_serves_the_original_and_reads_nothing_more(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, body.decode()), (200, X_TEXT))
        self.assertEqual((len(self.worker.jobs), len(self.worker.picked_early)), (0, 0))   # no audio read started

    def test_a_busy_file_known_to_have_no_track_lists_its_original(self):
        # found in review: for a viewer whose choice is off (the audio route off, or their own answer), a pick
        # of a file with no track of its own can only serve its original, so once that is known it is listed as
        # "(original)"; while its tracks are unknown, or it has one, the "(Subsync)" entry stays (it serves the
        # copy once ready)
        http(self.base + self.URL)                                       # the job is queued
        with self.worker.cv:
            self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}

        def listed():
            return [(s["label"], s["variantOf"]) for s in json.loads(http(self.base + self.URL)[1])["subtitles"]]
        self.assertEqual(listed(), [("English (Subsync)", "os-9")])      # its tracks aren't known yet
        self.store.alias(self.ALIAS, FILE)
        self.store.file_read(FILE, 5, FILE[:16], False, {"reference": False})   # its first read: no track
        self.assertTrue(self.worker.busy_with(self.ALIAS))
        self.assertEqual(listed(), [("English (original)", "os-9")])
        self.s.audio_route, self.s.audio_when = True, "off"              # a person who chose off, the route on
        self.assertEqual(listed(), [("English (original)", "os-9")])
        self.store.file_read(FILE, 5, FILE[:16], True, {"reference": True})     # a track: its copy comes
        self.assertEqual(listed(), [("English (Subsync)", "os-9")])

    def test_a_finished_copy_is_listed_as_synced_without_a_subsync_entry(self):
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        self.store.alias(self.ALIAS, FILE)
        self.store.file_read(FILE, 5, FILE[:16], True, {})               # read just now: no new job
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertEqual([s["label"] for s in subs], ["English (synced)"])


class ListFixture(AddonCase):
    """The addon's server over a worker that is not started, whose file is listed at a server that holds no file: a read
    of it fails at once (HTTP 404), and `files.headers` records it. No tests of its own."""

    def setUp(self):
        watch_the_log(self)
        self.files = no_file_server(self)
        self.aios = FakeAios()
        self.aios.row = {"url": self.files.url("b.mkv")}
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_LANGUAGES": "eng,rus",
                         "SUBSYNC_PICK_WAIT_MS": "300"}, self.aios)
        self.serve_addon()


class EmptyListTest(ListFixture):
    """A subtitle list that comes back empty (a slow subtitle addon left out of AIOStreams' answer) is not a
    "none found": nothing is remembered, and a pick asks for the list again instead of serving the note."""
    ALIAS = "name:b.mkv|5"

    def test_an_empty_list_is_not_remembered_as_no_subtitle(self):
        job = Job("movie", "tt9", {"filename": "b.mkv", "videoSize": 5})
        self.worker.submit(job)
        self.worker.jobs.clear()
        self.worker.queue.clear()
        self.worker.process(job)                                        # the list comes back empty
        self.assertIsNone(self.worker.known_originals(self.ALIAS))
        self.assertIsNone(self.store.originals_get(self.ALIAS))
        self.assertEqual(self.files.headers, [])                        # nothing of the file asked for
        self.assertEqual(self.store.day_count("read"), 0)

    def test_a_pick_after_an_empty_list_asks_for_the_list_again(self):
        job = Job("movie", "tt9", {"filename": "b.mkv", "videoSize": 5})
        self.worker.submit(job)
        self.worker.jobs.clear()
        self.worker.queue.clear()
        self.worker.process(job)
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Retry-After")), (503, "1"))   # not the "none found" note
        self.assertEqual([(j.alias, j.type, j.video_id) for j in self.worker.jobs.values()], [(self.ALIAS, "movie", "tt9")])


class BestGuessFallbackTest(ListFixture):
    """While there is no copy, a download of subsync's entry serves the subtitle likeliest to fit the file
    as it is (a hash match, the release group, the kind of source), not the viewer's first: on Detectorists S01E01 that was
    a DVD rip 41.7 s off, where AutoSync served the WEB subtitle that fits by its name."""
    NAME = "Show.S01E01.WEBRip.x264-GRP.mkv"

    def setUp(self):
        super().setUp()
        cands = [{"id": "os-dvd", "url": "http://subs/dvd.srt", "lang": "eng", "release": "Show S01E01 DVDRip NonHI"},
                 {"id": "os-web", "url": "http://subs/web.srt", "lang": "eng", "release": "Show.S01E01.WEB"},
                 {"id": "os-rus", "url": "http://subs/rus.srt", "lang": "rus", "release": "Show"},
                 {"id": "os-hash", "url": "http://subs/hash.srt", "lang": "rus", "release": "Show", "hash": "ab12"}]
        self.aios.subtitles = lambda *a: [dict(c) for c in cands]

    def originals(self, hints, **env):
        for k, v in env.items():
            setattr(self.s, k, v)
        job = Job("series", "tt9:1:1", dict({"filename": self.NAME, "videoSize": 5}, **hints))
        # the list is known; the read then fails, at once (at an address that never answered, each read was
        # tried again after READ_RETRY_S, 30 s of the module's run)
        self.worker.process(job)
        return {lang: (o or {}).get("id") for lang, o in self.worker.known_originals(job.alias).items()}

    def test_the_likeliest_fit_is_served_while_there_is_no_copy(self):
        self.assertEqual(self.originals({}), {"eng": "os-web", "rus": "os-rus"})
        self.assertEqual(self.originals({"videoHash": "AB12"}), {"eng": "os-web", "rus": "os-hash"})

    def test_ranking_off_keeps_the_viewers_first(self):
        self.assertEqual(self.originals({}, rank_by_release=False), {"eng": "os-dvd", "rus": "os-rus"})


class BestFitOffersTest(AddonCase):
    """(2), (3), (6): the passing results of a file and language are offered best fit first; the main language
    lists up to 3 of them, the others 1; a pick (the "(Subsync)" entry) waits up to 14 s for the result."""

    def setUp(self):
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_LANGUAGES": "eng,rus"})

    def put(self, sha, lang, rank):
        return self.store.put(FILE, sha * 64, lang, rank, "synced", body=f"1\n00:00:0{rank + 1},000 --> 00:00:09,000\n{sha}\n",
                              fmt="srt", offer=False)

    def test_defaults(self):
        self.assertEqual(self.s.pick_wait, 14.0)                # AIOMetadata gives up on a subtitle after 15 s
        self.assertTrue(self.s.best_fit and self.s.skip_ai and self.s.prefer_plain)
        self.assertEqual(self.s.alternatives, 3)

    def test_offers_follow_the_given_order_and_the_first_is_the_one_a_pick_serves(self):
        a, b, c = self.put("a", "eng", 0), self.put("b", "eng", 1), self.put("c", "eng", 2)
        self.assertIsNone(self.store.last_good(FILE, "eng"))   # offer=False: nothing offered until the ranking is set
        self.store.set_offers(FILE, "eng", [c, a, b])
        self.assertEqual(self.store.last_good(FILE, "eng")["key"], c)
        self.assertEqual([r["key"] for r in self.store.offers(FILE, ["eng"], {"eng": 3})], [c, a, b])
        self.assertEqual([r["key"] for r in self.store.offers(FILE, ["eng"])], [c])   # one per language by default
        self.store.set_offers(FILE, "eng", [])
        self.assertIsNone(self.store.last_good(FILE, "eng"))

    def test_the_main_language_lists_up_to_three_the_others_one(self):
        keys = [self.put(x, "eng", i) for i, x in enumerate("abcd")]
        self.store.set_offers(FILE, "eng", keys)
        rus = [self.put(x, "rus", i) for i, x in enumerate("ef")]
        self.store.set_offers(FILE, "rus", rus)
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": self.s.rule_id})   # read just now: no new job
        subs = self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual([s["label"] for s in subs],
                         ["English (synced)", "English (synced 2)", "English (synced 3)", "Russian (synced)"])
        self.assertEqual(len({s["id"] for s in subs}), 4)

    def test_fit_ranking(self):
        exact = {"outcome": "synced", "reason": "in sync as it is (viewer's #3): listed unchanged",
                 "detail": {"gate_original": {"median_ms": 0.0, "within200": 1.0}}}
        snapped = {"outcome": "synced", "reason": "passes against the release track (snapped)",
                   "detail": {"gate_synced": {"median_ms": 60.0, "within200": 0.86}}}
        snapped_worse = {"outcome": "synced", "reason": "passes against the release track",
                         "detail": {"gate_synced": {"median_ms": 90.0, "within200": 0.91}}}
        better = {"outcome": "synced", "reason": "clearly better than the original",
                  "detail": {"gate_synced": {"median_ms": 150.0, "within200": 0.6}}}
        speech = {"outcome": "synced", "reason": "clearly better than the original (speech)",
                  "detail": {"gate_synced": {"median_ms": 107.0, "within200": 0.64}}}
        speech_best = dict(speech, detail={"gate_synced": {"median_ms": 95.0, "within200": 0.64}})
        ranked = sorted([better, snapped_worse, exact, snapped], key=Worker.fit_rank)
        self.assertEqual(ranked, [exact, snapped_worse, snapped, better])   # unchanged first; then share, then median
        self.assertEqual(sorted([speech, speech_best], key=Worker.fit_rank), [speech_best, speech])

    def test_how_a_copy_passed_is_read_from_its_recorded_gates(self):
        # The gates its judging recorded (detail["better"], detail["aligned"]) say how a copy passed; the
        # reason's words only for a row stored without them, so rewording a reason changes no ranking
        main = {"outcome": "synced", "reason": "re-timed to the release",
                "detail": {"gate_synced": {"median_ms": 90.0, "within200": 0.7}}}
        better = {"outcome": "synced", "reason": "re-timed to the release",
                  "detail": {"gate_synced": {"median_ms": 50.0, "within200": 0.9}, "better": {"pass": True}}}
        self.assertEqual(sorted([better, main], key=Worker.fit_rank), [main, better])   # the main rule first
        shift = {"outcome": "synced", "reason": "moved by one shift",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 0.95}, "shift_fit": {"offset_s": 1.0}}}
        alass = {"outcome": "synced", "reason": "passes against the release track",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 1.0}}}
        self.assertEqual(sorted([alass, shift], key=Worker.fit_rank), [shift, alass])   # its author's own timing
        aligned = dict(shift, detail=dict(shift["detail"], aligned={"pass": True}))
        self.assertEqual(sorted([aligned, alass], key=Worker.fit_rank), [alass, aligned])   # not by the main rule
        old = {"outcome": "synced", "reason": "clearly better than the original",   # stored before the gates were kept
               "detail": {"gate_synced": {"median_ms": 50.0, "within200": 0.9}}}
        self.assertEqual(sorted([old, main], key=Worker.fit_rank), [main, old])

    def test_a_subtitle_moved_by_one_shift_ranks_with_the_unchanged_ones(self):
        # Game of Thrones S01E01: an in-sync original (91.6 % within 200 ms) kept a BluRay rip, 1 s off and exact after one
        # shift, from being tried; a whole-subtitle shift keeps its author's timing, so it ranks by fit with the originals
        original = {"outcome": "synced", "reason": "in sync as it is (viewer's #8): lines snapped to the release's own",
                    "detail": {"gate_original": {"median_ms": 56.0, "within200": 0.916}, "magnet": {}}}
        rip = {"outcome": "synced", "reason": "passes against the release track (one shift) (snapped)",
               "detail": {"gate_synced": {"median_ms": 1.0, "within200": 1.0}, "shift_fit": {"offset_s": -0.999}}}
        weak_shift = {"outcome": "synced", "reason": "clearly better than the original (one shift)",
                      "detail": {"gate_synced": {"median_ms": 150.0, "within200": 0.7}, "shift_fit": {"offset_s": 3.0}}}
        alass = {"outcome": "synced", "reason": "passes against the release track (snapped)",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 1.0}}}
        self.assertEqual(sorted([alass, weak_shift, original, rip], key=Worker.fit_rank), [rip, original, alass, weak_shift])

    def test_a_stacked_subtitle_ranks_after_plain_ones_of_its_class(self):
        # The Fellowship of the Ring: a fan subtitle with Elvish transcriptions laid over the English lines (7 % of its
        # lines start while another is on screen) fit best by a hair and was the first download
        layered = {"outcome": "synced", "reason": "in sync as it is (viewer's #6): lines snapped to the release's own",
                   "detail": {"gate_original": {"median_ms": 0.0, "within200": 0.997}, "magnet": {}, "stacked": 0.074}}
        plain = {"outcome": "synced", "reason": "passes against the release track (one shift) (snapped)",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 0.99}, "shift_fit": {"offset_s": -0.1}, "stacked": 0.0}}
        alass = {"outcome": "synced", "reason": "passes against the release track (snapped)",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 1.0}}}
        self.assertEqual(sorted([alass, layered, plain], key=Worker.fit_rank), [plain, layered, alass])
        far_worse = dict(plain, detail=dict(plain["detail"], gate_synced={"median_ms": 0.0, "within200": 0.91}))
        self.assertEqual(sorted([far_worse, layered], key=Worker.fit_rank), [layered, far_worse])   # not after a far worse one


class RuleChangeTest(AddonCase):
    """A verdict judged under another pass rule (or other rule settings) is judged again."""

    def setUp(self):
        self.make_addon({"SUBSYNC_PUBLIC_BASE": "http://subsync:8790"})

    def test_the_rule_id_follows_the_pass_rule_settings(self):
        base = Settings({}).rule_id
        self.assertEqual(base, Settings({}).rule_id)
        for env in ({"SUBSYNC_BETTER_MEDIAN_MS": "150"}, {"SUBSYNC_BETTER": "off"}, {"SUBSYNC_SNAP_WITHIN": "0.9"},
                    {"SUBSYNC_AUDIO_BETTER": "off"}, {"SUBSYNC_AUDIO_BETTER_MEDIAN_MS": "120"},
                    {"SUBSYNC_AUDIO_BUDGET_GB": "2"}):
            self.assertNotEqual(Settings(env).rule_id, base, env)
        self.assertEqual(Settings({"SUBSYNC_LANGUAGES": "eng,rus"}).rule_id, base)   # not a pass rule setting

    def test_a_kept_original_judged_under_another_rule_is_judged_again(self):
        key = self.store.put(FILE, SHA_A, "eng", 0, "keep_original", "below the bar", detail={"rule": "older"})
        self.assertIsNone(self.worker.fresh(key))
        self.store.put(FILE, SHA_A, "eng", 0, "keep_original", "below the bar", detail={"rule": self.s.rule_id})
        self.assertIsNotNone(self.worker.fresh(key))
        synced = self.store.put(FILE, SHA_B, "eng", 1, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                                fmt="srt", route="audio", detail={"rule": "older"})
        self.assertIsNotNone(self.worker.fresh(synced))                  # an audio-route copy that passed stays offered

    def test_a_result_from_the_files_own_track_judged_under_another_rule_is_judged_again(self):
        # the index is read anyway: a copy made against a typesetting track before that track was refused, or an in-sync
        # original listed before the magnet, is made again under the rule in force
        for outcome, route in (("synced", "cues"), ("in_sync", "cues"), ("synced", "image")):
            key = self.store.put(FILE, SHA_C, "eng", 1, outcome, body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                                 fmt="srt", route=route, detail={"rule": "older"})
            self.assertIsNone(self.worker.fresh(key), (outcome, route))
            self.store.put(FILE, SHA_C, "eng", 1, outcome, body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                           fmt="srt", route=route, detail={"rule": self.s.rule_id})
            self.assertIsNotNone(self.worker.fresh(key), (outcome, route))

    def test_a_file_read_under_another_rule_is_queued_again_at_once(self):
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": "older"})
        self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(self.worker.jobs), 1)
        self.worker.jobs.clear()
        self.worker.queue.clear()
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": self.s.rule_id})
        self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(self.worker.jobs), 0)                       # read under this rule within 24 h
