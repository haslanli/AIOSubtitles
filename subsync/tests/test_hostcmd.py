"""The two commands ./setup runs inside the service's container: the numbers of the page
after setup, in public mode too (people signed up, read today), and a copy of the databases before an update."""
import ast
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout

import helpers  # noqa: F401  (first: it puts the package on the import path)
from subsync import hostcmd
from subsync.settings import Settings
from subsync.store import Store


def users_schema():
    """users.SCHEMA, the people's tables as the service makes them, read from users.py's source: importing users needs
    cryptography, and hostcmd (standard library only) and these tests don't."""
    with open(os.path.join(os.path.dirname(hostcmd.__file__), "users.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "SCHEMA" for target in node.targets))


USERS_SCHEMA = users_schema()


def people(folder, n):
    """A users database with n people, in the service's own tables."""
    with closing(sqlite3.connect(os.path.join(folder, "users.sqlite3"))) as db:
        db.executescript(USERS_SCHEMA)
        db.executemany("INSERT INTO users (id, blob, created_day, seen_day) VALUES (?, ?, ?, ?)",
                       [(f"u{i}", b"x", "2026-11-13", "2026-11-13") for i in range(n)])
        db.commit()


class HostCmdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_cmd(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = hostcmd.main(list(argv), env=dict(env or {}, SUBSYNC_DATA=self.data))
        return code, out.getvalue(), err.getvalue()

    def open_store_with_a_write(self, n=1234):
        """The service's store, open as while it runs, with a write of n bytes read today that is still only in its
        write-ahead log (not checkpointed into the database file)."""
        store = Store(self.data)
        self.addCleanup(store.close)
        store.day_add("read", n)
        self.assertGreater(os.path.getsize(os.path.join(self.data, "subsync.sqlite3-wal")), 0)
        return store

    def test_status_counts_people_and_todays_reads(self):
        people(self.data, 3)
        store = Store(self.data)
        store.day_add("read", 2_500_000)
        store.close()
        code, out, _ = self.run_cmd("status", env={"SUBSYNC_PUBLIC": "on", "SUBSYNC_MAX_USERS": "150"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"public": True, "people": 3, "max_people": 150, "read_today": 2_500_000,
                                           "daily_budget": Settings({}).daily_budget})

    def test_the_databases_copied_are_the_files_the_service_opens(self):
        # The store and main_public named their files apart from this list; a rename would have left that
        # database out of every copy before an update, with no error (a file that isn't there is skipped)
        from subsync import store
        self.assertEqual(hostcmd.DATABASES, (store.USERS_DATABASE, store.DATABASE))
        Store(self.data).close()
        self.assertTrue(os.path.isfile(os.path.join(self.data, store.DATABASE)))

    def test_status_of_a_new_install_is_zero(self):
        code, out, _ = self.run_cmd("status")
        self.assertEqual(json.loads(out)["people"], 0)
        self.assertEqual(json.loads(out)["read_today"], 0)
        self.assertEqual(os.listdir(self.data), [])                       # read-only: nothing made

    def test_status_while_the_service_runs_counts_its_last_write(self):
        people(self.data, 2)
        self.open_store_with_a_write(1234)
        code, out, err = self.run_cmd("status")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((json.loads(out)["people"], json.loads(out)["read_today"]), (2, 1234))

    def test_copy_databases_while_the_service_runs_holds_its_last_write(self):
        people(self.data, 2)
        self.open_store_with_a_write(1234)
        dest = os.path.join(self.data, "before-update")
        code, out, err = self.run_cmd("copy-databases", dest)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(sorted(json.loads(out)["copied"]), ["subsync.sqlite3", "users.sqlite3"])
        with closing(sqlite3.connect(os.path.join(dest, "subsync.sqlite3"))) as db:
            self.assertEqual(db.execute("SELECT bytes FROM budget WHERE kind='read'").fetchall(), [(1234,)])

    def test_copy_databases_copies_the_databases_there_are(self):
        dest = os.path.join(self.data, "before-update")
        code, out, _ = self.run_cmd("copy-databases", dest)               # a new install: none yet
        self.assertEqual((code, json.loads(out)["copied"], os.listdir(dest)), (0, [], []))
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        Store(self.data).close()                                           # self-hosted: no people's database
        code, out, _ = self.run_cmd("copy-databases", dest)
        self.assertEqual((code, json.loads(out)["copied"], os.listdir(dest)), (0, ["subsync.sqlite3"],
                                                                               ["subsync.sqlite3"]))

    def test_a_damaged_copy_stops_with_what_is_wrong(self):
        people(self.data, 3)
        with closing(sqlite3.connect(os.path.join(self.data, "users.sqlite3"))) as db:
            db.execute("CREATE INDEX by_day ON users(created_day)")
            db.commit()
            db.execute("PRAGMA writable_schema=ON")                       # the index no longer matches its table
            db.execute("UPDATE sqlite_master SET sql='CREATE INDEX by_day ON users(seen_day, id)' WHERE name='by_day'")
            db.commit()
        dest = os.path.join(self.data, "before-update")
        with self.assertRaises(SystemExit) as stop:
            self.run_cmd("copy-databases", dest)
        self.assertTrue(str(stop.exception).startswith("The copy of users.sqlite3 is damaged: row 1 missing from index"),
                        stop.exception)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dest, "users.sqlite3")).st_mode), 0o600)

    def test_copy_databases_makes_whole_private_copies(self):
        people(self.data, 2)
        Store(self.data).close()
        dest = os.path.join(self.data, "before-update")
        code, out, _ = self.run_cmd("copy-databases", dest)
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
        for argv in (["delete-everything"], [], ["status", "now"], ["copy-databases"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cmd(*argv)
                self.assertEqual((code, out, err), (2, "", hostcmd.USAGE + "\n"))

    def test_a_data_folder_whose_name_has_uri_characters_is_read_as_it_is(self):
        # The read-only address was "file:<path>?mode=ro" as written: '#' or '?' cut it (SQLite then opened
        # and made another file, read-write), '%' changed it
        for name in ("e#f", "g?h", "d%41b"):
            with self.subTest(name=name):
                data = os.path.join(self.data, name)
                os.makedirs(data)
                people(data, 2)
                Store(data).close()
                before = sorted(os.listdir(self.data))
                out = io.StringIO()
                with redirect_stdout(out):
                    code = hostcmd.main(["status"], env={"SUBSYNC_DATA": data})
                self.assertEqual((code, json.loads(out.getvalue())["people"]), (0, 2))
                dest = os.path.join(data, "copy")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(hostcmd.main(["copy-databases", dest], env={"SUBSYNC_DATA": data}), 0)
                with closing(sqlite3.connect(os.path.join(dest, "users.sqlite3"))) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
                self.assertEqual(sorted(os.listdir(self.data)), before)          # nothing made beside it

    def test_reading_settings_loads_none_of_the_sync(self):
        # Settings imported lang_list from the worker, so `hostcmd status` (and anything else that only reads
        # settings) loaded the worker, the sync's 20-odd modules and numpy: "Standard library only" above was false
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        probe = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport subsync.hostcmd\nfrom subsync.settings import "
                 "Settings\nSettings({})\nprint(sorted(m for m in sys.modules if m == 'numpy' or m.startswith("
                 "('numpy.', 'subsync.worker', 'subsync.pipeline', 'subsync.audiofit'))))")
        run = subprocess.run([sys.executable, "-I", "-c", probe, root], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=60, env={"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        self.assertEqual(run.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
