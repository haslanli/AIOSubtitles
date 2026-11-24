"""Public mode's records of the people signed up: encrypted with a key from the service's secret and the person's own
link, so the database alone, or the database and the secret, opens nothing."""
import contextlib
import os
import sqlite3
import threading
import unittest
from unittest import mock

from helpers import Clock, files_bytes, HAVE_CRYPTOGRAPHY, NEEDS, TempDir

SECRET = bytes(range(32))
RECORD = {"instance": "https://aios.test", "sync_uuid": "11111111-2222-3333-4444-555555555555",
          "sync_link": "https://aios.test/stremio/11111111-2222-3333-4444-555555555555/ENCRYPTEDPART/manifest.json",
          "sync_password": "sync-password-made-by-the-service", "languages": ["eng", "rus"], "version": 1}
DAY = 86400


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class UsersTest(unittest.TestCase):
    def setUp(self):
        from subsync import users
        self.users_module = users
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.path = os.path.join(self.dir, "users.sqlite3")
        self.clock = Clock()
        self.users = users.Users(self.path, SECRET, clock=self.clock)
        self.token = users.new_token()

    def tearDown(self):
        self.users.close()
        self.tmp.__exit__(None, None, None)

    def test_token_shape(self):
        self.assertRegex(self.token, self.users_module.TOKEN_RE)
        self.assertEqual(len(self.token), 43)
        self.assertNotEqual(self.token, self.users_module.new_token())

    def test_round_trip(self):
        self.users.create(self.token, RECORD)
        self.assertEqual(self.users.open(self.token), RECORD)
        self.assertEqual(self.users.count(), 1)
        changed = dict(RECORD, languages=["eng"])
        self.assertTrue(self.users.update(self.token, changed))
        self.assertEqual(self.users.open(self.token)["languages"], ["eng"])

    def test_another_token_or_secret_opens_nothing(self):
        self.users.create(self.token, RECORD)
        self.assertIsNone(self.users.open(self.users_module.new_token()))
        self.assertIsNone(self.users.open("not a token"))
        other = self.users_module.Users(self.path, bytes(32), clock=self.clock)
        try:
            self.assertIsNone(other.open(self.token))
            self.assertNotEqual(other.user_id(self.token), self.users.user_id(self.token))
        finally:
            other.close()

    def test_a_read_key_gives_back_its_token_and_nothing_else(self):
        # The key of a person's subtitle entries, sealed with the secret, so nothing is stored
        key = self.users.read_key(self.token)
        self.assertRegex(key, f"^{self.users_module.READ_KEY}$")
        self.assertNotIn(self.token, key)
        self.assertEqual(self.users.read_key(self.token), key)             # the same in every list
        self.assertEqual(self.users.token_of(key), self.token)
        self.assertNotEqual(self.users.read_key(self.users_module.new_token()), key)
        other = self.users_module.Users(self.path, bytes(32), clock=self.clock)
        try:
            self.assertIsNone(other.token_of(key))                         # another secret
            self.assertNotEqual(other.read_key(self.token), key)
        finally:
            other.close()
        changed = key[:10] + ("A" if key[10] != "A" else "B") + key[11:]
        for made_up in (changed, "A" * 79, key[:-1], self.token, "", None, "!" * 79):
            with self.subTest(key=made_up):
                self.assertIsNone(self.users.token_of(made_up))
        self.assertEqual(self.users.count(), 0)                            # nothing was stored

    def test_the_module_says_what_a_record_holds(self):
        # It said a record never holds the person's own AIOStreams password; with "Keep my password" it does
        doc = " ".join(self.users_module.__doc__.split())
        self.assertNotIn("never their own AIOStreams password", doc)
        for held in ("the id of their config", "the Usenet config", "each one's id, link and password",
                     "their answer for files with no subtitles of their own", "their own TMDB key",
                     "what the last check of their AIOStreams found",
                     "their own AIOStreams password only while they tick the box that lets AIOSubtitles keep it"):
            self.assertIn(held, doc)

    def test_the_database_file_holds_no_secret(self):
        self.users.create(self.token, RECORD)
        self.users.add_usage(self.token, files=1, nbytes=1000)
        self.users.close()
        data = files_bytes(self.dir)
        for secret in [self.token, RECORD["sync_link"], RECORD["sync_password"], RECORD["sync_uuid"], RECORD["instance"],
                       "ENCRYPTEDPART"]:
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(secret.encode(), data)
        self.users = self.users_module.Users(self.path, SECRET, clock=self.clock)

    def test_a_tampered_blob_opens_nothing(self):
        self.users.create(self.token, RECORD)
        db = sqlite3.connect(self.path)
        blob = bytearray(db.execute("SELECT blob FROM users").fetchone()[0])
        blob[-1] ^= 1
        db.execute("UPDATE users SET blob=?", (bytes(blob),))
        db.commit()
        db.close()
        self.assertIsNone(self.users.open(self.token))

    def test_delete_removes_usage_too(self):
        self.users.create(self.token, RECORD)
        self.users.add_usage(self.token, files=2, nbytes=10)
        self.assertTrue(self.users.delete(self.token))
        self.assertIsNone(self.users.open(self.token))
        self.assertEqual(self.users.usage(self.token), {"files": 0, "bytes": 0})
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)
        self.assertFalse(self.users.delete(self.token))

    def test_expire_after_keep_days(self):
        old, recent = self.token, self.users_module.new_token()
        self.users.create(old, RECORD)
        self.clock.t += 2 * DAY
        self.users.create(recent, RECORD)
        self.clock.t += 179 * DAY        # old: seen 181 days ago; recent: 179
        self.assertEqual(self.users.expire(180), 1)
        self.assertIsNone(self.users.open(old))
        self.assertIsNotNone(self.users.open(recent))

    def test_expire_deletes_in_one_transaction_however_many_go(self):
        # Each record that went took four statements, each its own commit and disk sync, all under the lock
        # every usage count waits on
        tables = self.users_module.DAY_TABLES
        gone = [self.users_module.new_token() for _ in range(5)]
        for token in gone:
            self.users.create(token, RECORD)
            self.users.add_usage(token, files=1, nbytes=1)
            self.users.add_usenet_listing(token)
            self.users.add_audio(token)
        self.clock.t += 100 * DAY
        self.users.create(self.token, RECORD)
        self.users.add_usage(self.token, files=1, nbytes=1)        # older than yesterday when expire runs: goes too
        self.clock.t += 85 * DAY                                    # the five: seen 185 days ago; this one: 85
        self.users.add_usage(self.token, files=2, nbytes=2)        # today's: kept
        self.users.add_audio(self.token)
        statements = []
        self.users.db.set_trace_callback(statements.append)
        self.assertEqual(self.users.expire(180), 5)
        self.users.db.set_trace_callback(None)
        self.assertEqual([s.split()[0] for s in statements], ["BEGIN"] + ["DELETE"] * (len(tables) + 1) + ["COMMIT"])
        self.assertEqual([self.users.open(t) for t in gone], [None] * 5)
        self.assertIsNotNone(self.users.open(self.token))
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual({t: db.execute(f"SELECT id, day FROM {t}").fetchall() for t in tables},
                             {"usage": [(self.users.user_id(self.token), self.users_module.day_of(self.clock.t))],
                              "usenet_usage": [],
                              "audio_usage": [(self.users.user_id(self.token), self.users_module.day_of(self.clock.t))]})
        self.assertEqual(self.users.usage(self.token), {"files": 2, "bytes": 2})
        self.assertEqual(self.users.expire(180), 0)

    def test_a_persons_day_rows_are_kept_from_yesterday_on(self):
        # Each person's daily use (bytes and files read, Usenet listings, audio syncs) was kept 40 days, though
        # nothing reads any day but today's; yesterday's is kept for the change of day
        self.assertEqual(self.users_module.USAGE_KEPT_S, DAY)
        self.users.create(self.token, RECORD)
        start = self.clock.t
        for day in range(3):                                    # the day before yesterday, yesterday, today
            self.clock.t = start + day * DAY
            self.users.add_usage(self.token, files=1, nbytes=1)
            self.users.add_usenet_listing(self.token)
            self.users.add_audio(self.token)
        self.assertEqual(self.users.expire(180), 0)
        kept = [self.users_module.day_of(start + day * DAY) for day in (1, 2)]
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            for table in self.users_module.DAY_TABLES:
                self.assertEqual(sorted(day for (day,) in db.execute(f"SELECT day FROM {table}")), kept, table)
        self.assertEqual(self.users.usage(self.token), {"files": 1, "bytes": 1})          # today's, as before

    def test_the_day_a_person_signed_up_isnt_kept(self):
        # Each record's row kept its sign-up day in clear beside the encrypted record, and nothing read it
        self.users.create(self.token, RECORD)
        day = self.users_module.day_of(self.clock.t)
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT created_day, seen_day FROM users").fetchall(), [("", day)])
            # the way back after a deploy: an earlier image still writes its four values, the day among them
            db.execute("INSERT INTO users VALUES (?,?,?,?)", ("0" * 64, b"x", day, day))
            db.commit()
        self.users.close()
        self.users = self.users_module.Users(self.path, SECRET, clock=self.clock)        # the next start empties it
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT created_day, seen_day FROM users").fetchall(), [("", day)] * 2)
        self.assertEqual(self.users.open(self.token), RECORD)

    def test_delete_removes_all_of_a_record_or_nothing(self):
        # Delete committed each of its statements on its own, so one that failed left the rest done
        self.users.create(self.token, RECORD)
        self.users.add_usage(self.token, files=2, nbytes=10)
        tables = self.users_module.DAY_TABLES
        with mock.patch.object(self.users_module, "DAY_TABLES", tables + ("no_such_table",)):
            with self.assertRaises(sqlite3.OperationalError):
                self.users.delete(self.token)
        self.assertFalse(self.users.db.in_transaction)
        self.assertEqual(self.users.open(self.token), RECORD)
        self.assertEqual(self.users.usage(self.token), {"files": 2, "bytes": 10})
        statements = []
        self.users.db.set_trace_callback(statements.append)
        self.assertTrue(self.users.delete(self.token))
        self.users.db.set_trace_callback(None)
        self.assertEqual([s.split()[0] for s in statements], ["BEGIN"] + ["DELETE"] * (len(tables) + 1) + ["COMMIT"])
        self.assertIsNone(self.users.open(self.token))

    def test_a_keep_days_below_1_deletes_nothing(self):
        # 0 deleted every record not used today, less than 0 every record, a sign-up of a minute ago included
        self.users.create(self.token, RECORD)
        self.clock.t += DAY
        for keep_days in (0, -1, 1.5, "180"):
            with self.subTest(keep_days=keep_days):
                with self.assertRaises(ValueError):
                    self.users.expire(keep_days)
                self.assertIsNotNone(self.users.open(self.token))
        self.assertEqual(self.users.expire(1), 0)                          # seen a day ago: kept for a whole day
        self.clock.t += DAY
        self.assertEqual(self.users.expire(1), 1)

    def test_a_persons_rows_key_is_made_from_their_link_alone(self):
        # The key of a person's own rows in the shared store: the same for their token each time, another for
        # another token or secret, and not their record's id (stored), so only their link leads to it
        key = self.users.rows_key(self.token)
        self.assertRegex(key, "^[0-9a-f]{32}$")
        self.assertEqual(self.users.rows_key(self.token), key)
        self.assertNotEqual(self.users.rows_key(self.users_module.new_token()), key)
        self.assertNotIn(key[:16], self.users.user_id(self.token))
        self.assertNotIn(self.users.user_id(self.token)[:16], key)
        other = self.users_module.Users(self.path, bytes(32), clock=self.clock)
        try:
            self.assertNotEqual(other.rows_key(self.token), key)
        finally:
            other.close()
        self.assertEqual(self.users.count(), 0)                            # nothing was stored

    def test_a_persons_who_key_is_made_from_their_link_alone(self):
        # The key of their counts in the sites' fair shares and of their Usenet files not found was the first
        # half of their record's id, so a copy of the data folder joined those rows to their record
        key = self.users.who_key(self.token)
        self.assertRegex(key, "^[0-9a-f]{16}$")
        self.assertEqual(self.users.who_key(self.token), key)
        self.assertNotEqual(self.users.who_key(self.users_module.new_token()), key)
        for stored in (self.users.user_id(self.token), self.users.rows_key(self.token)):
            self.assertNotIn(key, stored)
        other = self.users_module.Users(self.path, bytes(32), clock=self.clock)
        try:
            self.assertNotEqual(other.who_key(self.token), key)
        finally:
            other.close()
        self.assertEqual(self.users.count(), 0)                            # nothing was stored

    def test_touch_keeps_a_record(self):
        self.users.create(self.token, RECORD)
        self.clock.t += 170 * DAY
        self.users.touch(self.token)
        self.clock.t += 170 * DAY
        self.assertEqual(self.users.expire(180), 0)

    def test_usage_is_per_utc_day(self):
        self.users.create(self.token, RECORD)
        self.users.add_usage(self.token, files=1, nbytes=500)
        self.users.add_usage(self.token, files=1, nbytes=250)
        self.assertEqual(self.users.usage(self.token), {"files": 2, "bytes": 750})
        self.clock.t += DAY
        self.assertEqual(self.users.usage(self.token), {"files": 0, "bytes": 0})

    def test_usage_starts_again_at_00_00_utc_not_24_hours_later(self):
        # The test above moves a whole day, which a rolling 24-hour window or a day in local time passes too
        midnight = 1_799_971_200.0                                       # 2027-01-15 00:00:00 UTC
        self.users.create(self.token, RECORD)
        self.clock.t = midnight + 60
        self.users.add_usage(self.token, files=1, nbytes=500)
        self.clock.t = midnight + DAY - 60                               # 23:59 UTC, the same day: still counted
        self.users.add_usage(self.token, files=1, nbytes=250)
        self.assertEqual(self.users.usage(self.token), {"files": 2, "bytes": 750})
        self.clock.t = midnight + DAY + 60                               # two minutes later, the next UTC day
        self.assertEqual(self.users.usage(self.token), {"files": 0, "bytes": 0})

    def free(self, token):
        """Whether the person's record lock can be taken at once (it is let go again)."""
        lock = self.users.locked(token)
        if not lock.acquire(blocking=False):
            return False
        lock.release()
        return True

    def test_each_person_has_their_own_record_lock(self):
        # Everyone shared 64 locks, so one person's refresh on a slow AIOStreams held up other people's saves;
        # of 65 people, two shared one
        people = [self.users_module.new_token() for _ in range(65)]
        for token in people:
            with self.users.locked(token):
                self.assertFalse(self.free(token))                     # the same person's next action waits
                self.assertEqual(sum(not self.free(other) for other in people if other != token), 0)
        self.assertEqual(len(self.users._record_locks), 0)             # none kept once nobody holds one

    def test_a_person_gets_one_lock_however_many_ask_at_once(self):
        barrier, got = threading.Barrier(16), []

        def ask():
            barrier.wait(5)
            got.append(self.users.locked(self.token))
        threads = [threading.Thread(target=ask, daemon=True) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(got), 16)
        self.assertEqual(len({id(lock) for lock in got}), 1)


if __name__ == "__main__":
    unittest.main()
