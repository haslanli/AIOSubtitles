"""Public mode: the people signed up, one encrypted record each, in their own database next to the shared store.

A person's personal link carries a token (32 random bytes, base64url) that is never stored. Their record is found by
an id made from the service's secret and that token, and encrypted (AES-256-GCM) with a key made from the secret and
the token, so neither the database, nor the database and the secret together, open a record without the person's link.
The service opens a record only while that person's requests come in. The subtitle entries their apps get carry a read
key instead of the token (read_key): the token sealed with the secret, which opens only those entries.

A record holds their AIOStreams address and the id of their config; the configs the service made there (the sync config
and, with Usenet on, the Usenet config: each one's id, link and password); their languages, their answer for files with
no subtitles of their own and their own TMDB key if they added one; what the last check of their AIOStreams found, and
when; and their own AIOStreams password only while they tick the box that lets AIOSubtitles keep it. Usage is
counted per person and UTC day for their limits.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import weakref

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import DAY, day_of

TOKEN = r"[A-Za-z0-9_-]{43}"     # a personal token: secrets.token_urlsafe(32), 43 base64url characters
TOKEN_RE = re.compile(f"^{TOKEN}$")
# the read key of a person's subtitle entries: their token sealed (AES-SIV: a 16-byte tag, then its 43 bytes), base64url
READ_KEY = r"[A-Za-z0-9_-]{79}"
READ_KEY_RE = re.compile(f"^{READ_KEY}$")
_SALT = b"aiosubtitles users v1"
_ENTRIES_INFO = b"subtitle entries"   # the read keys' own key (a token's characters have no space: never the same info)
_FORMAT = b"\x01"
_NONCE = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, blob BLOB NOT NULL, created_day TEXT NOT NULL,
  seen_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (id TEXT NOT NULL, day TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (id, day));
CREATE TABLE IF NOT EXISTS usenet_usage (id TEXT NOT NULL, day TEXT NOT NULL, listings INTEGER NOT NULL DEFAULT 0,
  stopped INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (id, day));
CREATE TABLE IF NOT EXISTS audio_usage (id TEXT NOT NULL, day TEXT NOT NULL, syncs INTEGER NOT NULL DEFAULT 0,
  opened INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (id, day));
"""
# usenet_usage and audio_usage (audio syncs started, and of those started as soon as a file was opened) are
# tables of their own: the usage table keeps its four columns, so an earlier image, whose usage insert names none,
# still works on this database (the way back after a deploy)
DAY_TABLES = ("usage", "usenet_usage", "audio_usage")


def new_token():
    """A personal link's token: 32 random bytes, base64url without padding (43 characters)."""
    return secrets.token_urlsafe(32)


class Users:
    def __init__(self, path, secret, clock=time.time):
        if len(secret) < 32:
            raise ValueError("the secret must be at least 32 bytes")
        self.secret, self.clock = bytes(secret), clock
        self._entries = AESSIV(HKDF(algorithm=hashes.SHA256(), length=64, salt=_SALT,
                                    info=_ENTRIES_INFO).derive(self.secret))
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        # one lock per person, kept only while someone holds it or waits for it
        self._record_locks = weakref.WeakValueDictionary()
        self._record_locks_guard = threading.Lock()

    def locked(self, token):
        """The lock of one person's record, held around reading it, asking AIOStreams and writing it back, so a refresh
        and the daily check never write over each other's changes. Each person has their own: one person's slow
        AIOStreams never holds up anyone else."""
        uid = self.user_id(token)
        with self._record_locks_guard:
            return self._record_locks.setdefault(uid, threading.Lock())

    def close(self):
        with self.lock:
            self.db.close()

    # -- ids and keys ------------------------------------------------------------------------------------------
    def user_id(self, token):
        return hmac.new(self.secret, b"user:" + token.encode(), hashlib.sha256).hexdigest()

    def _key(self, token):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=token.encode()).derive(self.secret)

    def read_key(self, token):
        """The key in the addresses of a person's subtitle entries: their token sealed with the service's
        secret, the same each time, so nothing is stored. It opens their entries only (public.py), never what their
        personal link opens; that link stays between them, their AIOStreams and the service."""
        return base64.urlsafe_b64encode(self._entries.encrypt(token.encode(), None)).decode().rstrip("=")

    def token_of(self, read_key):
        """The token a read key was made from; None for anything else (made up, or sealed with another secret)."""
        if not isinstance(read_key, str) or not READ_KEY_RE.match(read_key):
            return None
        try:
            token = self._entries.decrypt(base64.urlsafe_b64decode(read_key + "="), None).decode()
        except (InvalidTag, ValueError):     # its tag doesn't match, or it isn't base64 or UTF-8
            return None
        return token if TOKEN_RE.match(token) else None

    def _seal(self, token, uid, record):
        nonce = os.urandom(_NONCE)
        data = json.dumps(record, sort_keys=True).encode()
        return _FORMAT + nonce + AESGCM(self._key(token)).encrypt(nonce, data, uid.encode())

    def _unseal(self, token, uid, blob):
        if not blob or blob[:1] != _FORMAT:
            return None
        nonce, sealed = blob[1:1 + _NONCE], blob[1 + _NONCE:]
        try:
            return json.loads(AESGCM(self._key(token)).decrypt(nonce, sealed, uid.encode()))
        except (InvalidTag, ValueError):
            return None

    # -- records -----------------------------------------------------------------------------------------------
    def create(self, token, record):
        uid = self.user_id(token)
        today = day_of(self.clock())
        with self.lock:
            self.db.execute("INSERT INTO users VALUES (?,?,?,?)", (uid, self._seal(token, uid, record), today, today))

    def open(self, token):
        """The person's record, or None (unknown, deleted, another secret, or changed on disk)."""
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return None
        uid = self.user_id(token)
        with self.lock:
            row = self.db.execute("SELECT blob FROM users WHERE id=?", (uid,)).fetchone()
        return self._unseal(token, uid, row[0]) if row else None

    def update(self, token, record):
        uid = self.user_id(token)
        with self.lock:
            cur = self.db.execute("UPDATE users SET blob=?, seen_day=? WHERE id=?",
                                  (self._seal(token, uid, record), day_of(self.clock()), uid))
        return cur.rowcount == 1

    def delete(self, token):
        uid = self.user_id(token)
        with self.lock:
            cur = self.db.execute("DELETE FROM users WHERE id=?", (uid,))
            for table in DAY_TABLES:
                self.db.execute(f"DELETE FROM {table} WHERE id=?", (uid,))
        return cur.rowcount == 1

    def touch(self, token):
        with self.lock:
            self.db.execute("UPDATE users SET seen_day=? WHERE id=?", (day_of(self.clock()), self.user_id(token)))

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def expire(self, keep_days):
        """Deletes the records of people with no request for more than keep_days days; returns how many."""
        cutoff = day_of(self.clock() - keep_days * DAY)
        with self.lock:
            gone = [uid for (uid,) in self.db.execute("SELECT id FROM users WHERE seen_day < ?", (cutoff,))]
            for uid in gone:
                self.db.execute("DELETE FROM users WHERE id=?", (uid,))
                for table in DAY_TABLES:
                    self.db.execute(f"DELETE FROM {table} WHERE id=?", (uid,))
            for table in DAY_TABLES:
                self.db.execute(f"DELETE FROM {table} WHERE day < ?", (day_of(self.clock() - 40 * DAY),))
        return len(gone)

    # -- usage per UTC day -------------------------------------------------------------------------------------
    def add_usage(self, token, files=0, nbytes=0):
        with self.lock:
            self.db.execute("INSERT INTO usage VALUES (?,?,?,?) ON CONFLICT(id, day) DO UPDATE SET "
                            "files=files+excluded.files, bytes=bytes+excluded.bytes",
                            (self.user_id(token), day_of(self.clock()), int(files), int(nbytes)))

    def usage(self, token):
        with self.lock:
            row = self.db.execute("SELECT files, bytes FROM usage WHERE id=? AND day=?",
                                  (self.user_id(token), day_of(self.clock()))).fetchone()
        return {"files": row[0], "bytes": row[1]} if row else {"files": 0, "bytes": 0}

    # -- Usenet listings per UTC day ----------------------------------------------------------------------------
    def add_usenet_listing(self, token):
        with self.lock:
            self.db.execute("INSERT INTO usenet_usage (id, day, listings) VALUES (?,?,1) ON CONFLICT(id, day) DO UPDATE "
                            "SET listings=listings+1", (self.user_id(token), day_of(self.clock())))

    def stop_usenet(self, token):
        """No more Usenet listings for this person today: their indexer or Usenet service answered with an error."""
        with self.lock:
            self.db.execute("INSERT INTO usenet_usage (id, day, stopped) VALUES (?,?,1) ON CONFLICT(id, day) DO UPDATE "
                            "SET stopped=1", (self.user_id(token), day_of(self.clock())))

    def usenet_usage(self, token):
        with self.lock:
            row = self.db.execute("SELECT listings, stopped FROM usenet_usage WHERE id=? AND day=?",
                                  (self.user_id(token), day_of(self.clock()))).fetchone()
        return {"listings": row[0], "stopped": bool(row[1])} if row else {"listings": 0, "stopped": False}

    # -- audio syncs per UTC day --------------------------------------------------------------------------
    def add_audio(self, token, opened=False):
        """An audio sync started: one more today, and with `opened` one more of those started as soon as a file was
        opened."""
        with self.lock:
            self.db.execute("INSERT INTO audio_usage (id, day, syncs, opened) VALUES (?,?,1,?) ON CONFLICT(id, day) DO "
                            "UPDATE SET syncs=syncs+1, opened=opened+excluded.opened",
                            (self.user_id(token), day_of(self.clock()), 1 if opened else 0))

    def audio_usage(self, token):
        with self.lock:
            row = self.db.execute("SELECT syncs, opened FROM audio_usage WHERE id=? AND day=?",
                                  (self.user_id(token), day_of(self.clock()))).fetchone()
        return {"syncs": row[0], "opened": row[1]} if row else {"syncs": 0, "opened": 0}
