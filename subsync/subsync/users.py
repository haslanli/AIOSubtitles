"""Public mode: the people signed up, one encrypted record each, in their own database next to the shared store.

A person's personal link carries a token (32 random bytes, base64url) that is never stored. Their record is found by
an id made from the service's secret and that token, and encrypted (AES-256-GCM) with a key made from the secret and
the token, so neither the database, nor the database and the secret together, open a record without the person's link.
The service opens a record only while that person's requests come in.

A record holds their AIOStreams address, the sync config the service made there (its id, link and password) and their
languages; never their own AIOStreams password. Usage is counted per person and UTC day for their limits.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SALT = b"aiosubtitles users v1"
_FORMAT = b"\x01"
_NONCE = 12
DAY = 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, blob BLOB NOT NULL, created_day TEXT NOT NULL,
  seen_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (id TEXT NOT NULL, day TEXT NOT NULL, files INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (id, day));
"""


def new_token():
    """A personal link's token: 32 random bytes, base64url without padding (43 characters)."""
    return secrets.token_urlsafe(32)


def day_of(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t))


class Users:
    def __init__(self, path, secret, clock=time.time):
        if len(secret) < 32:
            raise ValueError("the secret must be at least 32 bytes")
        self.secret, self.clock = bytes(secret), clock
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._record_locks = [threading.Lock() for _ in range(64)]

    def locked(self, token):
        """A lock for one person's record, held around reading it, asking AIOStreams and writing it back, so a refresh
        and the daily check never write over each other's changes."""
        return self._record_locks[int(self.user_id(token)[:8], 16) % len(self._record_locks)]

    def close(self):
        with self.lock:
            self.db.close()

    # -- ids and keys ------------------------------------------------------------------------------------------
    def user_id(self, token):
        return hmac.new(self.secret, b"user:" + token.encode(), hashlib.sha256).hexdigest()

    def _key(self, token):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=token.encode()).derive(self.secret)

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
            self.db.execute("DELETE FROM usage WHERE id=?", (uid,))
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
                self.db.execute("DELETE FROM usage WHERE id=?", (uid,))
            self.db.execute("DELETE FROM usage WHERE day < ?", (day_of(self.clock() - 40 * DAY),))
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
