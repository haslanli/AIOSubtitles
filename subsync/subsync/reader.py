"""Bounded HTTP range reads of one release file.

Every read is one bounded `Range: bytes=a-b` request with `Connection: close`, read to its length and closed, so no
connection outlives its read. One info log line per read, naming the job's label, never the URL. The file's link (an
AIOStreams playback address, which redirects to the debrid service's download address) is followed once: later
reads go straight to the address it pointed to, kept in memory for this reader only, so a hundred small reads are not a
hundred calls to AIOStreams; when that address stops answering, the link is asked once more, and a 429 there is a
refusal like any other. Once the link has redirected, this reader asks it at most 1 + MAX_RE_RESOLVE times in all
: after that a read the address can't answer fails without asking AIOStreams again (a connection fault
at the download address had made 50 calls). The Real-Debrid guard checks every address, that one too. A refusal
(HTTP 403, most likely a debrid per-account download limit) raises Refused so the worker can back off; a
reply whose total size differs from the file's raises ReadFailed. Before each read the guard
(budget, pause) may stop the job.
"""
import logging
import threading
import time
import urllib.error
import urllib.request

from . import realdebrid

log = logging.getLogger("subsync.read")

USER_AGENT = "subsync/1"


MAX_RE_RESOLVE = 3     # look-ups of a redirecting link per reader after its first


class ReadFailed(Exception):
    """The file can't be read as asked (HTTP error, wrong size, short reply)."""


class LinkUsedUp(ReadFailed):
    """This read would have to ask a link that was asked 1 + MAX_RE_RESOLVE times already; no request made."""


class Refused(ReadFailed):
    """HTTP 403 (or 429): the provider refuses this account for now; back off before any other read."""


class Stopped(Exception):
    """The guard stopped the job before a read (budget spent, a viewer is streaming)."""


class RangeReader:
    def __init__(self, url, label, guard=None, timeout=60.0, size=None, size_tolerance=0.0, opener=None, size_slack=0,
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
        self.bytes = 0
        self.requests = 0
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
        look-up. When that address doesn't answer (an error status other than 429, no connection), it is dropped and
        the link asked once more, while look-ups are left (`used_up`); a 429 there, or a failure when none are left,
        reaches the caller like the link's own errors (urllib.error.HTTPError, OSError), so the worker backs off."""
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
            except (OSError, ValueError) as e:
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
        data, status, total, sent = b"", 0, None, True
        try:
            r, t0 = self._open(start, length)
            with r:
                status = getattr(r, "status", 200)
                content_range = r.headers.get("Content-Range") or ""
                if "/" in content_range and content_range.rsplit("/", 1)[1].strip().isdigit():
                    total = int(content_range.rsplit("/", 1)[1])
                elif status == 200:
                    length_header = r.headers.get("Content-Length")
                    total = int(length_header) if length_header and length_header.isdigit() else None
                # read at most what was asked, even when a server ignores the range (status 200)
                chunks, left = [], length
                while left > 0:
                    chunk = r.read(min(1 << 20, left))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    left -= len(chunk)
                data = b"".join(chunks)
        except LinkUsedUp:
            sent = False
            raise
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
        except (OSError, ValueError) as e:
            self._log(start, length, 0, status, t0, type(e).__name__)
            raise ReadFailed(type(e).__name__) from None
        finally:
            if sent:
                self._count(requests=1)
        self._count(got=len(data))
        if self.guard:
            self.guard.after_read(len(data))
        self._log(start, length, len(data), status, t0, None)
        if status in (403, 429):
            raise Refused(f"HTTP {status}")
        if status not in (200, 206):
            raise ReadFailed(f"HTTP {status}")
        if status == 200 and start != 0:
            raise ReadFailed("range ignored")
        if total is not None:
            if self.size is None:
                if self.size_ok is not None and not self.size_ok(total):
                    raise ReadFailed("size differs")
                self.size = total
            elif abs(total - self.size) > max(self.tolerance * self.size, self.slack):
                raise ReadFailed("size differs")
            else:
                self.size = total
        expected = min(length, self.size - start) if self.size is not None else length
        if len(data) < expected:
            raise ReadFailed("short reply")
        return data

    def stream(self, start, length, chunk=256 * 1024):
        """One bounded range request read in chunks as the caller takes them; a caller that stops early (closes the
        generator) closes the connection, and only the bytes read are counted. The same checks as get()."""
        if length <= 0:
            return
        if self.size is not None and start >= self.size:
            raise ReadFailed("range past the end")
        if self.size is not None:
            length = min(length, self.size - start)
        if self.guard:
            self.guard.before_read(length)
        t0 = time.time()
        try:
            r, t0 = self._open(start, length)
        except urllib.error.HTTPError as e:
            self._count(requests=1)
            e.close()
            self._log(start, length, 0, e.code, t0, None)
            if e.code in (403, 429):
                raise Refused(f"HTTP {e.code}") from None
            raise ReadFailed(f"HTTP {e.code}") from None
        except (OSError, ValueError) as e:
            self._count(requests=1)
            self._log(start, length, 0, 0, t0, type(e).__name__)
            raise ReadFailed(type(e).__name__) from None
        self._count(requests=1)
        got, status = 0, getattr(r, "status", 200)
        try:
            if status == 200 and start != 0:
                raise ReadFailed("range ignored")
            content_range = r.headers.get("Content-Range") or ""
            if "/" in content_range and content_range.rsplit("/", 1)[1].strip().isdigit():
                total = int(content_range.rsplit("/", 1)[1])
                if self.size is None:
                    self.size = total
                elif abs(total - self.size) > max(self.tolerance * self.size, self.slack):
                    raise ReadFailed("size differs")
                else:
                    self.size = total
            left = length
            while left > 0:
                data = r.read(min(chunk, left))
                if not data:
                    raise ReadFailed("short reply")
                got += len(data)
                left -= len(data)
                yield data
        finally:
            r.close()
            self._count(got=got)
            if self.guard:
                self.guard.after_read(got)
            self._log(start, length, got, status, t0, None)

    def _count(self, got=0, requests=0):
        with self._lock:
            self.bytes += got
            self.requests += requests

    def _log(self, start, length, got, status, t0, error):
        log.info("read %s bytes=%d-%d got=%d status=%s ms=%d%s", self.label, start, start + length - 1, got,
                 status or "-", int((time.time() - t0) * 1000), f" error={error}" if error else "")
