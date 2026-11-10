"""python -m subsync.selftest: one index read of one file, for an installer's self-test.

Reads the stream link from standard input (never from the command line, so it doesn't show in process lists), applies
SUBSYNC_URL_REWRITE as the worker does, reads what subsync reads first for any file (the start, the seek index and the
64 KiB at each end for the file's hash), at most --cap-mb, and prints one JSON line: whether it is Matroska, its size,
the subtitle tracks found, the bytes and requests used. Never prints the link.
"""
import argparse
import json
import sys
import time

from .mkv import MkvIndex, NotMatroska
from .reader import RangeReader, ReadFailed, Refused, Stopped
from .settings import Settings
from .worker import rewrite


class CapGuard:
    def __init__(self, cap):
        self.cap, self.used = cap, 0

    def before_read(self, length):
        if self.used + length > self.cap:
            raise Stopped("cap reached")

    def after_read(self, n):
        self.used += n


def run(url, cap_bytes, settings, size=None):
    guard = CapGuard(cap_bytes)
    reader = RangeReader(rewrite(url, settings.url_rewrite), "selftest", guard, timeout=settings.read_timeout,
                         size=size, size_tolerance=0.0005)
    t0 = time.time()
    out = {"ok": False}
    try:
        index = MkvIndex(reader).read_head().read_cues()
        index.read_hash()
        text = index.text_tracks()
        out.update(ok=True, matroska=True, size=index.size,
                   text_tracks=[{"language": t["language"], "lines": len(t["cues"]), "forced": t["forced"]} for t in text],
                   image_tracks=len(index.image_tracks()))
    except NotMatroska:
        out.update(ok=True, matroska=False, note="not a Matroska file: subsync keeps the original subtitles for it")
    except Refused as e:
        out.update(error="refused", detail=str(e))
    except Stopped:
        out.update(error="cap", detail=f"stopped at the {cap_bytes // (1024 * 1024)} MB cap")
    except ReadFailed as e:
        out.update(error="read", detail=str(e))
    out.update(bytes=reader.bytes, requests=reader.requests, ms=int((time.time() - t0) * 1000))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cap-mb", type=float, default=4.0)
    ap.add_argument("--size", type=int, default=None, help="the size the listing gave, checked against the reply")
    a = ap.parse_args()
    url = sys.stdin.readline().strip()
    if not url.startswith(("http://", "https://")):
        print(json.dumps({"ok": False, "error": "no link on standard input"}))
        return 2
    out = run(url, int(a.cap_mb * 1024 * 1024), Settings(), a.size)
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
