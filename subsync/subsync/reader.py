"""Bounded HTTP range reads of one release file.

Every read is one bounded `Range: bytes=a-b` request with `Connection: close`, read to its length and closed, so no
connection outlives its read. One info log line per read, naming the job's label, never the URL. A refusal
(HTTP 403, most likely a debrid per-account download limit) raises Refused so the worker can back off; a
reply whose total size differs from the file's raises ReadFailed. Before each read the guard
(budget, pause) may stop the job.
"""
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("subsync.read")

USER_AGENT = "subsync/1"


class ReadFailed(Exception):
    """The file can't be read as asked (HTTP error, wrong size, short reply)."""


class Refused(ReadFailed):
    """HTTP 403 (or 429): the provider refuses this account for now; back off before any other read."""


class Stopped(Exception):
    """The guard stopped the job before a read (budget spent, a viewer is streaming)."""


class RangeReader:
    def __init__(self, url, label, guard=None, timeout=60.0, size=None, size_tolerance=0.0, opener=None, size_slack=0):
        self.url, self.label, self.guard, self.timeout = url, label, guard, timeout
        self.size = size              # expected served size (from the listing), checked on the first reply
        self.tolerance = size_tolerance
        # bytes the listed size may be off by rounding: addons list sizes to 0.01 GiB, so up to 0.005 GiB (5.4 MB),
        # which is 0.35 % of a 1.4 GiB episode and far over a relative tolerance
        self.slack = size_slack
        self.bytes = 0
        self.requests = 0
        self._lock = threading.Lock()   # audio windows are read up to 4 at once (audiofit.AudioFit.prefetch)
        self.open = opener or urllib.request.urlopen

    def get(self, start, length):
        if length <= 0:
            return b""
        if self.size is not None and start >= self.size:
            raise ReadFailed("range past the end")
        if self.size is not None:
            length = min(length, self.size - start)
        if self.guard:
            self.guard.before_read(length)
        req = urllib.request.Request(self.url, headers={
            "Range": f"bytes={start}-{start + length - 1}", "Connection": "close", "User-Agent": USER_AGENT})
        t0 = time.time()
        data, status, total = b"", 0, None
        try:
            with self.open(req, timeout=self.timeout) as r:
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
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
        except (OSError, ValueError) as e:
            self._log(start, length, 0, status, t0, type(e).__name__)
            raise ReadFailed(type(e).__name__) from None
        finally:
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
        req = urllib.request.Request(self.url, headers={
            "Range": f"bytes={start}-{start + length - 1}", "Connection": "close", "User-Agent": USER_AGENT})
        t0 = time.time()
        try:
            r = self.open(req, timeout=self.timeout)
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
