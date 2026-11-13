"""Two commands ./setup runs inside the service's container: python -m subsync.hostcmd status | copy-databases DEST.

status: the numbers the page after setup shows (people signed up and the limit, read today and the daily total), as
one JSON line. It reads the databases directly, read-only, so it needs no secret and changes nothing; public mode's
/status answers nobody, by design.

copy-databases DEST: a consistent copy of the people's records and of the shared store, made with SQLite's backup API
while the service runs, each checked with integrity_check, private (0600 in a 0700 folder). ./setup makes one before
an update of a public instance and copies it out of the container.

Standard library only.
"""
import json
import os
import sqlite3
import sys
from contextlib import closing

from .settings import Settings
from .store import day_of

DATABASES = ("users.sqlite3", "subsync.sqlite3")
USAGE = "usage: python -m subsync.hostcmd status | copy-databases DEST"


def _read_only(path):
    return closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True))


def numbers(settings):
    out = {"public": settings.public, "people": 0, "max_people": settings.max_users, "read_today": 0,
           "daily_budget": settings.daily_budget}
    users = os.path.join(settings.data, "users.sqlite3")
    if os.path.exists(users):
        with _read_only(users) as db:
            out["people"] = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    store = os.path.join(settings.data, "subsync.sqlite3")
    if os.path.exists(store):
        with _read_only(store) as db:
            row = db.execute("SELECT bytes FROM budget WHERE day=? AND kind='read'", (day_of(),)).fetchone()
            out["read_today"] = row[0] if row else 0
    return out


def copy_databases(data, dest):
    os.makedirs(dest, mode=0o700, exist_ok=True)
    os.chmod(dest, 0o700)
    copied = []
    for name in DATABASES:
        source = os.path.join(data, name)
        if not os.path.exists(source):
            continue
        target = os.path.join(dest, name)
        with _read_only(source) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        os.chmod(target, 0o600)
        if result != "ok":
            raise SystemExit(f"The copy of {name} is damaged: {result}")
        copied.append(name)
    return copied


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    settings = Settings(env)
    if argv == ["status"]:
        print(json.dumps(numbers(settings)))
        return 0
    if len(argv) == 2 and argv[0] == "copy-databases":
        print(json.dumps({"copied": copy_databases(settings.data, argv[1])}))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
