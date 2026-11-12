"""Public mode's records of the people signed up: encrypted with a key from the service's secret and the person's own
link, so the database alone, or the database and the secret, opens nothing."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import TempDir  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False

SECRET = bytes(range(32))
RECORD = {"instance": "https://aios.test", "sync_uuid": "11111111-2222-3333-4444-555555555555",
          "sync_link": "https://aios.test/stremio/11111111-2222-3333-4444-555555555555/ENCRYPTEDPART/manifest.json",
          "sync_password": "sync-password-made-by-the-service", "languages": ["eng", "rus"], "version": 1}
DAY = 86400


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
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

    def test_the_database_file_holds_no_secret(self):
        self.users.create(self.token, RECORD)
        self.users.add_usage(self.token, files=1, nbytes=1000)
        self.users.close()
        data = b"".join(open(os.path.join(self.dir, f), "rb").read() for f in os.listdir(self.dir))
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
        self.assertEqual(sqlite3.connect(self.path).execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)
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


if __name__ == "__main__":
    unittest.main()
