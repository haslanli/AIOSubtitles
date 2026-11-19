"""Bounded HTTP range reads of one release file.

Every read is one bounded `Range: bytes=a-b` request with `Connection: close`, read to its length and closed, so no
connection outlives its read. One info log line per read, naming the job's label, never the URL. The file's link (an
AIOStreams playback address, which redirects to the debrid service's download address) is followed once: later
reads go straight to the address it pointed to, kept in memory for this reader only, so a hundred small reads are not a
hundred calls to AIOStreams; when that address stops answering, the link is asked once more, and a 429 there is a
refusal like any other. Once the link has redirected, this reader asks it at most 1 + MAX_RE_RESOLVE times in all:
after that a read the address can't answer fails without asking AIOStreams again (a connection fault
at the download address had made 50 calls). The Real-Debrid guard checks every address, that one too. A refusal
(HTTP 403, most likely a debrid per-account download limit) raises Refused so the worker can back off; a
reply whose total size differs from the file's, or whose bytes start elsewhere than asked, raises ReadFailed. A read
that got no answer (no connection, a reset, a timeout, a reply cut short), or one that goes on past its time (the
timeout plus a second for every READ_BYTES_PER_S bytes asked for: "too slow"), raises ReadFailed with
`connection` set, so the worker can try a file's first read once more. Before each read the guard
(budget, pause) may stop the job; after each, one that failed too, it is told the bytes received and the length the
read asked for (guard.after_read). The bytes a read received are counted also when it fails: a reply is read
a CHUNK at a time, so what arrived before it broke off is known.
"""
import http.client
import io
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from . import net, realdebrid
from .version import user_agent

log = logging.getLogger("subsync.read")

USER_AGENT = user_agent("subsync")


MAX_RE_RESOLVE = 3     # look-ups of a redirecting link per reader after its first
# how far a served file's size may be from its listed one, as a share of it, besides the listing's rounding (size_slack)
SIZE_TOLERANCE = 0.0005
FIRST_BYTE = re.compile(r"\s*bytes\s+([0-9]+)-", re.IGNORECASE)   # a Content-Range's first byte
# a read may go on for the timeout plus one second for every READ_BYTES_PER_S bytes it asks for
READ_BYTES_PER_S = 64 * 1024
CHUNK = 256 * 1024     # bytes taken from a reply at a time, each counted as it arrives


class ReadFailed(Exception):
    """The file can't be read as asked (HTTP error, wrong size, short reply, no answer). `connection`: the request got
    no answer (no connection, a reset, a timeout), so the same read may work a moment later."""

    def __init__(self, *args, connection=False):
        super().__init__(*args)
        self.connection = connection


def connection_fault(e):
    """A request that got no answer it could use, so the same read may work a moment later: an OSError that is not an
    HTTP status nor one of AIOSubtitles' own refusals of an address (Real-Debrid, one that isn't public), which would
    refuse it again; or a reply http.client couldn't read (cut short, a garbled status line), but a malformed address."""
    if isinstance(e, http.client.HTTPException):
        return not isinstance(e, http.client.InvalidURL)
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return isinstance(e, OSError) and not isinstance(e, urllib.error.HTTPError) \
        and not isinstance(reason, (realdebrid.Blocked, net.Refused))


def reply_range(headers, status):
    """What a reply says it holds: (the offset of its first byte, the file's total size), each None when it doesn't
    say. Content-Range ("bytes A-B/T") gives both; a whole file (status 200) without a total there, its Content-Length."""
    content_range = headers.get("Content-Range") or ""
    first = FIRST_BYTE.match(content_range)
    total = content_range.rsplit("/", 1)[1].strip() if "/" in content_range else ""
    if not (total.isascii() and total.isdigit()):
        length = headers.get("Content-Length") if status == 200 else None
        total = length if length and length.isascii() and length.isdigit() else None
    return (int(first.group(1)) if first else None), (int(total) if total is not None else None)


class LinkUsedUp(ReadFailed):
    """This read would have to ask a link that was asked 1 + MAX_RE_RESOLVE times already; no request made."""


class Refused(ReadFailed):
    """HTTP 403 (or 429): the provider refuses this account for now; back off before any other read."""


class Stopped(Exception):
    """The guard stopped the job before a read (budget spent, a viewer is streaming)."""


class CapReached(Stopped):
    """The read would pass the job's own byte cap (`cap`, in bytes): final for the file under that cap,
    so the job isn't tried again later."""

    def __init__(self, cap):
        super().__init__("file byte cap")
        self.cap = cap


class IndexTooLarge(CapReached):
    """The file's index is larger than the parsers ever read (`cap`: mkv.MAX_CUES, mp4.MAX_MOOV), whatever the job's
    own cap: it ends as a cap does, the file marked and its original subtitles kept."""


class _TimeLimit:
    """Ends a read that goes on past `seconds`: the reader's timeout bounds each wait for data only, so a
    service that sends a byte now and then would hold a read for hours. A timer shuts the reply's connection down,
    which makes the read in progress return; `passed` then says so. A reply that has no connection of its own (one made
    in memory) has no limit."""

    def __init__(self, reply, seconds):
        self.passed = False
        self._lock = threading.Lock()
        try:   # a duplicate of the reply's socket: while it is held, the connection's number can't go to another one
            self._sock = socket.fromfd(reply.fileno(), socket.AF_INET, socket.SOCK_STREAM)
        except (AttributeError, OSError, TypeError, ValueError):
            self._sock = None
        self._timer = threading.Timer(seconds, self._end) if self._sock is not None else None

    def _end(self):
        with self._lock:
            if self._sock is not None:
                self.passed = True
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def __enter__(self):
        if self._timer is not None:
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, *exc):
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class RangeReader:
    def __init__(self, url, label, guard=None, timeout=60.0, size=None, size_tolerance=SIZE_TOLERANCE, opener=None,
                 size_slack=0,
                 max_re_resolve=MAX_RE_RESOLVE, first_timeout=None, size_ok=None):
        self.url, self.label, self.guard, self.timeout = url, label, guard, timeout
        # a Usenet file: its link asked at most twice (a fresh look-up could fetch its NZB again), and
        # its first look-up waits longer (the Usenet service fetches and opens the NZB first; NzbDAV's add takes up to
        # 90 s)
        self.max_re_resolve = max_re_resolve
        self.first_timeout = first_timeout
        # with no exact size: a rule the served size must meet when the first reply gives it (a Usenet release is
        # listed by its whole NZB, so its file is a little smaller than listed)
        self.size_ok = size_ok
        self.size = size              # expected served size (from the listing), checked on the first reply
        self.tolerance = size_tolerance
        # bytes the listed size may be off by rounding: addons list sizes to 0.01 GiB, so up to 0.005 GiB (5.4 MB),
        # which is 0.35 % of a 1.4 GiB episode and far over a relative tolerance
        self.slack = size_slack
        self.bytes = 0                # bytes received, also by reads that failed
        self.requests = 0
        self.finished = 0             # reads that got all they asked for and passed the reply's checks
        self._lock = threading.Lock()   # audio windows are read up to 4 at once (audiofit.AudioFit.prefetch)
        self.open = opener or realdebrid.urlopen   # refuses Real-Debrid addresses, redirects included
        self.resolved = None          # the address the link pointed to: never logged, never stored
        self.lookups = 0              # requests to the link itself (the first one and any after)
        self.redirects = False        # the link has redirected once: its look-ups are then counted
        self._resolving = threading.Lock()   # parallel first reads wait for one look-up of the link

    def _request(self, url, start, length):
        return urllib.request.Request(url, headers={
            "Range": f"bytes={start}-{start + length - 1}", "Connection": "close", "User-Agent": USER_AGENT})

    def _open(self, start, length):
        """The response to one range request and its start time: to the address the link pointed to last time,
        else to the link, which is then followed and the address it pointed to kept; reads started together wait for one
        look-up. When that address doesn't answer (an error status other than 429, no connection, a garbled reply), it is
        dropped and the link asked once more, while look-ups are left (`used_up`); a 429 there, or a failure when none
        are left, reaches the caller like the link's own errors (urllib.error.HTTPError, OSError,
        http.client.HTTPException), so the worker backs off."""
        resolved = self.resolved
        if resolved:
            t0 = time.time()
            try:
                return self.open(self._request(resolved, start, length), timeout=self.timeout), t0
            except realdebrid.Blocked:
                raise
            except urllib.error.HTTPError as e:
                # 429 is the debrid service slowing us down, and an address that keeps failing after fresh look-ups is
                # a refusal too: raised as such, so the worker backs off instead of asking the link again and again
                if e.code == 429 or self.used_up():
                    raise
                code = e.code
                e.close()
            except (OSError, ValueError, http.client.HTTPException) as e:
                if self.used_up():
                    raise
                code = type(e).__name__
            self._count(requests=1)
            self._log(start, length, 0, code, t0, "at the address the link pointed to: asking the link again")
            with self._lock:
                if self.resolved == resolved:
                    self.resolved = None
        with self._resolving:     # one look-up at a time: reads started together use the first one's answer
            if self.resolved:
                t0 = time.time()
                return self.open(self._request(self.resolved, start, length), timeout=self.timeout), t0
            if self.used_up():
                self._log(start, length, 0, None, time.time(), "the link was asked too often: not asked again")
                raise LinkUsedUp("the link was asked too often")
            t0 = time.time()
            first = self.lookups == 0
            self.lookups += 1
            r = self.open(self._request(self.url, start, length),
                          timeout=self.first_timeout if first and self.first_timeout else self.timeout)
            final = r.geturl() if hasattr(r, "geturl") else None
            if final and final != self.url:
                self.resolved, self.redirects = final, True
            return r, t0

    def _time_limit(self, reply, length):
        """The limit of a read of `length` bytes, from its reply on (_TimeLimit)."""
        return _TimeLimit(reply, self.timeout + length / READ_BYTES_PER_S)

    def used_up(self):
        """The link has redirected and been asked 1 + max_re_resolve times: it isn't asked again."""
        return self.redirects and self.lookups > self.max_re_resolve

    def get(self, start, length):
        if length <= 0:
            return b""
        if self.size is not None and start >= self.size:
            raise ReadFailed("range past the end")
        if self.size is not None:
            length = min(length, self.size - start)
        if self.guard:
            self.guard.before_read(length)
        t0 = time.time()
        data, got, status, first, total, sent, limit = b"", 0, 0, None, None, True, None
        try:
            r, t0 = self._open(start, length)
            with r, self._time_limit(r, length) as limit:
                status = getattr(r, "status", 200)
                first, total = reply_range(r.headers, status)
                # read at most what was asked, even when a server ignores the range (status 200), a CHUNK at a time so
                # what arrived is counted if the read breaks off, into one buffer whose bytes are taken
                # without a copy, so a large read (a moov, the Cues, an audio window) isn't held twice
                buf = io.BytesIO()
                while got < length:
                    piece = r.read(min(CHUNK, length - got))
                    if not piece:
                        break
                    got += buf.write(piece)
                data = buf.getvalue()
        except LinkUsedUp:
            sent = False
            raise
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
        except (OSError, ValueError, http.client.HTTPException) as e:
            slow = limit is not None and limit.passed
            self._log(start, length, got, status, t0, "too slow" if slow else type(e).__name__)
            raise ReadFailed("too slow" if slow else type(e).__name__, connection=slow or connection_fault(e)) from None
        finally:
            # also when it failed: what it received is counted, and the length it asked for is no longer reserved
            # (worker.JobGuard)
            self._count(got=got, requests=1 if sent else 0)
            if self.guard:
                self.guard.after_read(got, length)
        self._log(start, length, got, status, t0, None)
        self._check(status, start, first, total)
        expected = min(length, self.size - start) if self.size is not None else length
        if got < expected:
            if limit is not None and limit.passed:
                raise ReadFailed("too slow", connection=True)
            raise ReadFailed("short reply")
        self._count(finished=1)
        return data

    def _check(self, status, start, first, total):
        """A reply as get() and stream() take it (reply_range gives `first` and `total`): Refused for a refusal, else
        ReadFailed for another status, a range ignored or started elsewhere than asked (a proxy's block-aligned answer),
        or a total size that isn't the file's (the size_ok rule while the size isn't known, else the tolerance and the
        slack). A total that passes is the file's size from then on."""
        if status in (403, 429):
            raise Refused(f"HTTP {status}")
        if status not in (200, 206):
            raise ReadFailed(f"HTTP {status}")
        if status == 200 and start != 0:
            raise ReadFailed("range ignored")
        if status == 206 and first is not None and first != start:
            raise ReadFailed("range differs")
        if total is not None:
            if self.size is None:
                if self.size_ok is not None and not self.size_ok(total):
                    raise ReadFailed("size differs")
            elif abs(total - self.size) > max(self.tolerance * self.size, self.slack):
                raise ReadFailed("size differs")
            self.size = total

    def stream(self, start, length, chunk=CHUNK):
        """One bounded range request read in chunks as the caller takes them; a caller that stops early (closes the
        generator) closes the connection, and only the bytes read are counted. The same checks as get() (_check)."""
        if length <= 0:
            return
        if self.size is not None and start >= self.size:
            raise ReadFailed("range past the end")
        if self.size is not None:
            length = min(length, self.size - start)
        if self.guard:
            self.guard.before_read(length)
        t0, opened = time.time(), False
        try:
            r, t0 = self._open(start, length)
            opened = True
        except urllib.error.HTTPError as e:
            self._count(requests=1)
            e.close()
            self._log(start, length, 0, e.code, t0, None)
            if e.code in (403, 429):
                raise Refused(f"HTTP {e.code}") from None
            raise ReadFailed(f"HTTP {e.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            self._count(requests=1)
            self._log(start, length, 0, 0, t0, type(e).__name__)
            raise ReadFailed(type(e).__name__, connection=connection_fault(e)) from None
        finally:
            if not opened and self.guard:   # nothing read: the length it asked for is no longer reserved (JobGuard)
                self.guard.after_read(0, length)
        self._count(requests=1)
        got, status, error = 0, getattr(r, "status", 200), None
        try:
            self._check(status, start, *reply_range(r.headers, status))
            with self._time_limit(r, length) as limit:
                left = length
                while left > 0:
                    try:
                        data = r.read(min(chunk, left))
                    except (OSError, ValueError, http.client.HTTPException) as e:   # a reset or a timeout in the body
                        error = "too slow" if limit.passed else type(e).__name__
                        raise ReadFailed(error, connection=limit.passed or connection_fault(e)) from None
                    if not data:
                        if limit.passed:
                            error = "too slow"
                        raise ReadFailed(error or "short reply", connection=limit.passed)
                    got += len(data)
                    left -= len(data)
                    yield data
            self._count(finished=1)
        finally:
            r.close()
            self._count(got=got)
            if self.guard:
                self.guard.after_read(got, length)
            self._log(start, length, got, status, t0, error)

    def _count(self, got=0, requests=0, finished=0):
        with self._lock:
            self.bytes += got
            self.requests += requests
            self.finished += finished

    def _log(self, start, length, got, status, t0, error):
        log.info("read %s bytes=%d-%d got=%d status=%s ms=%d%s", self.label, start, start + length - 1, got,
                 status or "-", int((time.time() - t0) * 1000), f" error={error}" if error else "")
