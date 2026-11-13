"""The two commands ./setup runs inside the service's container: the numbers of the page
after setup, in public mode too (people signed up, read today), and a copy of the databases before an update."""
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import hostcmd  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.store import Store  # noqa: E402


def people(folder, n):
    with closing(sqlite3.connect(os.path.join(folder, "users.sqlite3"))) as db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, blob BLOB NOT NULL, created_day TEXT NOT NULL)")
        db.executemany("INSERT INTO users VALUES (?, ?, ?)", [(f"u{i}", b"x", "2026-11-13") for i in range(n)])
        db.commit()


class HostCmdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_cmd(self, *argv, env=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = hostcmd.main(list(argv), env=dict(env or {}, SUBSYNC_DATA=self.data))
        return code, out.getvalue()

    def test_status_counts_people_and_todays_reads(self):
        people(self.data, 3)
        store = Store(self.data)
        store.add_bytes("read", 2_500_000)
        store.db.close()
        code, out = self.run_cmd("status", env={"SUBSYNC_PUBLIC": "on", "SUBSYNC_MAX_USERS": "150"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"public": True, "people": 3, "max_people": 150, "read_today": 2_500_000,
                                           "daily_budget": Settings({}).daily_budget})

    def test_status_of_a_new_install_is_zero(self):
        code, out = self.run_cmd("status")
        self.assertEqual(json.loads(out)["people"], 0)
        self.assertEqual(json.loads(out)["read_today"], 0)

    def test_copy_databases_makes_whole_private_copies(self):
        people(self.data, 2)
        Store(self.data).db.close()
        dest = os.path.join(self.data, "before-update")
        code, out = self.run_cmd("copy-databases", dest)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(out)["copied"]), ["subsync.sqlite3", "users.sqlite3"])
        for name in ("users.sqlite3", "subsync.sqlite3"):
            path = os.path.join(dest, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with closing(sqlite3.connect(os.path.join(dest, "users.sqlite3"))) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)

    def test_an_unknown_command_says_how_to_use_it(self):
        code, out = self.run_cmd("delete-everything")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
