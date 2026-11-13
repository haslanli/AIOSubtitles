"""Matroska by range reads: head, tracks, the seek index (Cues), the OpenSubtitles hash and bounded audio windows.

mkvmerge writes one cue point per subtitle block (CueTime + CueDuration), so a text track's whole timing
comes from the Cues without reading any cluster. An audio window is read as the clusters that cover it,
planned from the Cues, prefixed with a minimal header (EBML + Segment of unknown size + Info + Tracks, no
attachments), and decoded from a pipe.
"""
import bisect
import logging
import struct
import subprocess
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

AUDIO_BYTES_LOCK = threading.Lock()


def add_audio_bytes(index, n):
    """Bytes of audio windows read for this file (its audio budget); windows may be read in parallel."""
    with AUDIO_BYTES_LOCK:
        index.audio_bytes = getattr(index, "audio_bytes", 0) + n


def add_words_bytes(index, n):
    """Bytes read for track lines' words (D1); lines are read in parallel."""
    with AUDIO_BYTES_LOCK:
        index.__dict__["words_bytes"] = index.__dict__.get("words_bytes", 0) + n


HEAD_BYTES = 256 * 1024
HASH_CHUNK = 64 * 1024
MAX_ELEMENT = 8 * 1024 * 1024      # Info/Tracks larger than this are not read
MAX_CUES = 32 * 1024 * 1024        # 7.27 MB on a file with 75 PGS tracks

EBML, SEGMENT, SEEKHEAD, INFO, TRACKS, CUES, CLUSTER = (0x1A45DFA3, 0x18538067, 0x114D9B74, 0x1549A966,
                                                        0x1654AE6B, 0x1C53BB6B, 0x1F43B675)
TEXT_CODECS = ("S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/WEBVTT", "S_TEXT/ASCII")
NOT_DIALOGUE = ("forced", "sign", "song", "karaoke", "commentary")


WORD_READ = 2048                   # bytes read for one line's words (D1)
IMAGE_READ = 64 * 1024             # an image line's block, at most: 2 KB first, the rest only when needed
WORD_READS_AT_ONCE = 4
MAX_FAILED_IN_A_ROW = 3            # a track's line reads stop after this many failed reads in a row

log = logging.getLogger("subsync.read")


class NotMatroska(Exception):
    pass


def block_payload(buf, track):
    """The payload of the SimpleBlock or BlockGroup's Block of `track` that starts within the first 8 bytes of buf (the
    Cluster's header is 5 to 12 bytes, so a block's place is known only to that), or None. The Cluster's size bytes can
    look like a block too: a payload found at two places that differ is no answer."""
    found = {_block_at(buf, o, track) for o in range(8)} - {None}
    return found.pop() if len(found) == 1 else None


def block_extent(buf, track):
    """(payload start, payload end) of the one block of `track` starting within buf's first 8 bytes; the end may lie
    past the buffer (the rest of the block not read yet). None when there is none, or more than one place fits."""
    found = {_block_extent(buf, o, track) for o in range(8)} - {None}
    return found.pop() if len(found) == 1 else None


def _block_extent(buf, o, track):
    try:
        eid, n = vint(buf, o, strip=False)
        if eid not in (0xA3, 0xA0):
            return None
        size, m = vint(buf, o + n)
        p = o + n + m
        if eid == 0xA0:
            cid, n2 = vint(buf, p, strip=False)
            if cid != 0xA1:
                return None
            size, m2 = vint(buf, p + n2)
            p += n2 + m2
        tn, k = vint(buf, p)
        if tn != track or size < k + 3 or size > MAX_ELEMENT:
            return None
        return p + k + 3, p + size
    except (IndexError, NotMatroska):
        return None


def _block_at(buf, o, track):
    """The payload of a block of `track` starting at buf[o], or None."""
    ext = _block_extent(buf, o, track)
    return None if ext is None else bytes(buf[ext[0]:ext[1]])


def uncompressed(payload, comp):
    """A block payload with the track's ContentCompression undone (zlib, or header stripping); None when it can't be."""
    if payload is None or not comp:
        return payload
    if comp["algo"] == 3:
        return comp["settings"] + payload
    if comp["algo"] == 0:
        try:
            return zlib.decompressobj().decompress(payload)
        except zlib.error:
            return None
    return None


def read_lines(want, one):
    """D1,: `one(item)` -> (start, a line's text or block payload, or None) for each item, WORD_READS_AT_ONCE
    at once; {start: value}. A refused read or a stopped job ends every read not started yet and is raised (`one`
    raises it and sets `stop` itself)."""
    from .reader import Refused, Stopped
    out = {}
    with ThreadPoolExecutor(max_workers=WORD_READS_AT_ONCE, thread_name_prefix="subsync-words") as pool:
        futures = [pool.submit(one, item) for item in want]
        for fut in futures:
            try:
                s, value = fut.result()
            except (Refused, Stopped):   # the host refused, or the guard stopped the job: no further read
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            if value:
                out[s] = value
    return out


class LineReads:
    """One track's line reads through `reader`, which stop for good once a read is refused or the job stops
    (both raised) or once MAX_FAILED_IN_A_ROW reads in a row have failed: a host that doesn't answer isn't asked for
    every line (a dead download address made 50 reads, each a call to the AIOStreams link). A read that works
    starts the count again, so a failure now and then stops nothing."""

    def __init__(self, reader):
        self.r = reader
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._failed = 0

    def get(self, at, n):
        """reader.get(at, n), or None when it failed or the reads have stopped."""
        from .reader import ReadFailed, Refused, Stopped
        if self.stop.is_set():
            return None
        try:
            buf = self.r.get(at, n)
        except (Refused, Stopped):
            self.stop.set()
            raise
        except ReadFailed:
            with self._lock:
                self._failed += 1
                if self._failed >= MAX_FAILED_IN_A_ROW and not self.stop.is_set():
                    self.stop.set()
                    log.info("line reads %s stopped: %d failed in a row", getattr(self.r, "label", "-"), self._failed)
            return None
        with self._lock:
            self._failed = 0
        return buf


def pipe_until(cmd, head, chunks, need, timeout=300):
    """Run a decoder on head + chunks (an iterator, e.g. RangeReader.stream) and stop feeding it once its output holds
    `need` bytes, or once it ends by itself; the iterator is then closed, which ends its read. Returns the output."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out, enough = bytearray(), threading.Event()

    def drain():
        while True:
            b = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(65536)
            if not b:
                break
            out.extend(b)
            if len(out) >= need:
                enough.set()
        enough.set()
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        p.stdin.write(head)
        for c in chunks:
            if enough.is_set():
                break
            p.stdin.write(c)
    except (BrokenPipeError, OSError):
        pass
    finally:
        if hasattr(chunks, "close"):
            chunks.close()
        try:
            p.stdin.close()
        except OSError:
            pass
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    reader.join(timeout)
    return bytes(out)


def vint(buf, pos, strip=True):
    if pos >= len(buf):
        raise IndexError("vint past the buffer")
    first = buf[pos]
    n = 1
    while n <= 8 and not (first & (0x80 >> (n - 1))):
        n += 1
    if n > 8:
        raise NotMatroska("bad vint")
    val = first & ((0xFF >> n) if strip else 0xFF)
    for b in buf[pos + 1: pos + n]:
        val = (val << 8) | b
    return val, n


def elements(buf, pos, end):
    """(id, element start, data start, data size) for each child element in buf[pos:end]."""
    while pos < end:
        try:
            eid, n = vint(buf, pos, strip=False)
            size, m = vint(buf, pos + n)
        except IndexError:
            return
        data = pos + n + m
        if size == (1 << (7 * m)) - 1:  # unknown size
            size = end - data
        yield eid, pos, data, size
        pos = data + size


def uint(b):
    return int.from_bytes(b, "big") if b else 0


def oshash(size, first, last):
    """OpenSubtitles hash: size plus the 64-bit little-endian sums of the first and last 64 KiB."""
    h = size
    for chunk in (first[:HASH_CHUNK], last[-HASH_CHUNK:]):
        for (q,) in struct.iter_unpack("<Q", chunk[: len(chunk) // 8 * 8]):
            h = (h + q) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


class MkvIndex:
    """What subsync needs from one file, read through a RangeReader."""

    def __init__(self, reader):
        self.r = reader
        self.size = None
        self.scale = 1_000_000
        self.duration_s = None
        self.tracks = []
        self.positions = {}
        self.cue_points = []     # (time s, absolute cluster position), any track
        self.track_cues = {}     # track number -> [(start s, duration s)]
        self.track_places = {}   # track number -> [(start s, cluster position, place in the cluster or None)]
        self.cues_at = None
        self.first_cluster = None
        self.ebml = self.info = self.tracks_raw = b""
        self.first64 = b""

    # -- reads -------------------------------------------------------------------------------------------------
    def read_head(self, head=None):
        head = self.r.get(0, HEAD_BYTES) if head is None else head
        self.size = self.r.size
        if self.size is None:
            raise NotMatroska("no size")
        self.first64 = head[:HASH_CHUNK]
        eid, n = vint(head, 0, strip=False)
        if eid != EBML:
            raise NotMatroska("not EBML")
        s, m = vint(head, n)
        ebml_end = n + m + s
        self.ebml = head[:ebml_end]
        eid, n = vint(head, ebml_end, strip=False)
        if eid != SEGMENT:
            raise NotMatroska("no Segment")
        _, m = vint(head, ebml_end + n)
        self.seg_data = ebml_end + n + m
        for eid, raw, p, sz in elements(head, self.seg_data, len(head)):
            if eid == CLUSTER:
                self.first_cluster = raw
                break
            if p + sz > len(head):
                if eid not in (SEEKHEAD, INFO, TRACKS) or p + sz - raw > MAX_ELEMENT:
                    break
                head = head[:raw] + self.r.get(raw, p + sz - raw)
            self._element(head, eid, raw, p, sz)
        for eid in (INFO, TRACKS):  # not in the first read: follow the SeekHead
            if eid in self.positions and not (self.info if eid == INFO else self.tracks_raw):
                self._fetch_element(self.positions[eid], eid)
        if not self.tracks:
            raise NotMatroska("no Tracks")
        return self

    def _fetch_element(self, at, want):
        hdr = self.r.get(at, 16)
        eid, n = vint(hdr, 0, strip=False)
        sz, m = vint(hdr, n)
        if eid != want or sz > MAX_ELEMENT:
            return
        body = self.r.get(at, n + m + sz)
        self._element(body, eid, 0, n + m, sz)

    def _element(self, buf, eid, raw, p, sz):
        if eid == SEEKHEAD:
            for _, _, sp, ssz in elements(buf, p, p + sz):
                sid = spos = None
                for cid, _, cp, csz in elements(buf, sp, sp + ssz):
                    if cid == 0x53AB:
                        sid = uint(buf[cp:cp + csz])
                    elif cid == 0x53AC:
                        spos = uint(buf[cp:cp + csz])
                if sid is not None and spos is not None:
                    self.positions.setdefault(sid, self.seg_data + spos)
        elif eid == INFO:
            self.info = bytes(buf[raw:p + sz])
            for cid, _, cp, csz in elements(buf, p, p + sz):
                if cid == 0x2AD7B1:
                    self.scale = uint(buf[cp:cp + csz])
                elif cid == 0x4489:
                    v = buf[cp:cp + csz]
                    self.duration_s = struct.unpack(">f", v)[0] if csz == 4 else struct.unpack(">d", v)[0]
            if self.duration_s is not None:
                self.duration_s = self.duration_s * self.scale / 1e9
        elif eid == TRACKS:
            self.tracks_raw = bytes(buf[raw:p + sz])
            for _, _, tp, tsz in elements(buf, p, p + sz):
                t = {"forced": 0, "default": 1, "language": "eng", "name": "", "codec": ""}
                for cid, _, cp, csz in elements(buf, tp, tp + tsz):
                    v = buf[cp:cp + csz]
                    if cid == 0xD7:
                        t["number"] = uint(v)
                    elif cid == 0x83:
                        t["type"] = uint(v)
                    elif cid == 0x86:
                        t["codec"] = v.decode(errors="replace")
                    elif cid == 0x22B59C:
                        t["language"] = v.decode(errors="replace")
                    elif cid == 0x22B59D:
                        t["bcp47"] = v.decode(errors="replace")
                    elif cid == 0x536E:
                        t["name"] = v.decode(errors="replace")
                    elif cid == 0x55AA:
                        t["forced"] = uint(v)
                    elif cid == 0x88:
                        t["default"] = uint(v)
                    elif cid == 0x63A2:   # CodecPrivate: VobSub's palette (the .idx header), ASS's header
                        t["private"] = bytes(v)
                    elif cid == 0x6D80:   # ContentEncodings: the compression of each block's payload
                        for _, _, ep, esz in elements(buf, cp, cp + csz):
                            for kid, _, kp, ksz in elements(buf, ep, ep + esz):
                                if kid != 0x5034:
                                    continue
                                comp = {"algo": 0, "settings": b""}
                                for zid, _, zp, zsz in elements(buf, kp, kp + ksz):
                                    if zid == 0x4254:
                                        comp["algo"] = uint(buf[zp:zp + zsz])
                                    elif zid == 0x4255:
                                        comp["settings"] = bytes(buf[zp:zp + zsz])
                                t["compression"] = comp
                if t.get("number"):
                    self.tracks.append(t)

    def read_cues(self):
        at = self.positions.get(CUES)
        if not at:
            return self
        hdr = self.r.get(at, 16)
        eid, n = vint(hdr, 0, strip=False)
        if eid != CUES:
            return self
        csize, m = vint(hdr, n)
        if csize > MAX_CUES:
            return self
        body = self.r.get(at + n + m, csize)
        self.cues_at = at
        seen = set()
        for _, _, pp, psz in elements(body, 0, len(body)):
            t = None
            for cid, _, cp, csz in elements(body, pp, pp + psz):
                if cid == 0xB3:
                    t = uint(body[cp:cp + csz]) * self.scale / 1e9
                elif cid == 0xB7 and t is not None:
                    trk = dur = cpos = rel = None
                    for gid, _, gp, gsz in elements(body, cp, cp + csz):
                        if gid == 0xF1:
                            cpos = self.seg_data + uint(body[gp:gp + gsz])
                            if cpos not in seen:
                                seen.add(cpos)
                                self.cue_points.append((t, cpos))
                        elif gid == 0xF7:
                            trk = uint(body[gp:gp + gsz])
                        elif gid == 0xB2:
                            dur = uint(body[gp:gp + gsz]) * self.scale / 1e9
                        elif gid == 0xF0:
                            rel = uint(body[gp:gp + gsz])
                    if trk is not None:
                        self.track_cues.setdefault(trk, []).append((t, dur or 0.0))
                        self.track_places.setdefault(trk, []).append((t, cpos, rel))
        self.cue_points.sort(key=lambda x: x[1])
        for lst in self.track_cues.values():
            lst.sort()
        for lst in self.track_places.values():
            lst.sort(key=lambda x: x[0])
        return self

    def read_hash(self):
        last = self.r.get(max(0, self.size - HASH_CHUNK), min(HASH_CHUNK, self.size))
        first = self.first64 if len(self.first64) >= min(HASH_CHUNK, self.size) else self.r.get(0, HASH_CHUNK)
        return oshash(self.size, first, last)

    # -- what the pipeline asks ---------------------------------------------------------------------------------
    def text_tracks(self):
        """Text subtitle tracks with their cue timings; forced/sign/song tracks marked."""
        out = []
        for t in self.tracks:
            if t.get("type") != 0x11 or not t.get("codec", "").startswith(TEXT_CODECS):
                continue
            name = t.get("name", "").lower()
            out.append({"number": t["number"], "language": t.get("bcp47") or t.get("language", "und"),
                        "codec": t["codec"], "name": t.get("name", ""),
                        "forced": bool(t.get("forced")) or any(w in name for w in NOT_DIALOGUE),
                        "cues": self.track_cues.get(t["number"], [])})
        return out

    def line_words(self, track, starts):
        """D1: the text of the lines of text track `track` that start at `starts` (seconds), each read from its
        block's place in the Cues (one read of WORD_READ bytes; WORD_READS_AT_ONCE at once). Returns {start: text} for
        those it could read; a line whose cue has no place in its cluster, or whose block doesn't parse, is left out. A
        refused read (Refused) or a stopped job (Stopped) is raised; after MAX_FAILED_IN_A_ROW failed reads in a row no
        further line is read (LineReads)."""
        meta = next((t for t in self.tracks if t.get("number") == track), {})
        comp = meta.get("compression")
        want = self._places(track, starts)
        reads = LineReads(self.r)

        def one(item):
            s, at = item
            if self.size is not None and at >= self.size:
                return s, None
            buf = reads.get(at, WORD_READ)
            if buf is None:
                return s, None
            add_words_bytes(self, len(buf))
            payload = uncompressed(block_payload(buf, track), comp)
            if payload is None:
                return s, None
            text = payload.decode("utf-8", "replace")
            if text.count("\ufffd") > 0.1 * max(1, len(text)):
                return s, None
            if meta.get("codec", "").startswith(("S_TEXT/ASS", "S_TEXT/SSA")):
                from .textpair import ass_text
                text = ass_text(text)
            return s, text
        return read_lines(want, one)

    def _places(self, track, starts):
        """[(start, the byte where its block's search begins)] for the starts whose cue has a place in its cluster: the
        Cluster's data start is 5 to 12 bytes after its position (block_extent looks at all eight)."""
        places = self.track_places.get(track, [])
        times = [p[0] for p in places]
        want = []
        for s in starts:
            k = bisect.bisect_left(times, s - 0.0015)
            if k < len(places) and abs(places[k][0] - s) <= 0.0015 and places[k][1] is not None \
                    and places[k][2] is not None:
                want.append((s, places[k][1] + 5 + places[k][2]))
        return want

    def codec_private(self, track):
        """A track's CodecPrivate (VobSub: the .idx header with its palette), b"" when it has none."""
        return next((t.get("private", b"") for t in self.tracks if t.get("number") == track), b"")

    def line_images(self, track, starts):
        """The block payloads (uncompressed) of the lines of image track `track` that start at `starts`: a first
        read of WORD_READ bytes, then the rest of the block when it is longer, never a block over IMAGE_READ bytes;
        WORD_READS_AT_ONCE lines at once. Returns {start: payload}. A refused read (Refused) or a stopped job (Stopped)
        is raised; after MAX_FAILED_IN_A_ROW failed reads in a row no further line is read (LineReads)."""
        meta = next((t for t in self.tracks if t.get("number") == track), {})
        comp = meta.get("compression")
        want = self._places(track, starts)
        reads = LineReads(self.r)

        def get(at, n):
            buf = reads.get(at, n)
            if buf is not None:
                add_words_bytes(self, len(buf))
            return buf

        def one(item):
            s, at = item
            if self.size is not None and at >= self.size:
                return s, None
            buf = get(at, min(WORD_READ, IMAGE_READ))
            ext = block_extent(buf, track) if buf else None
            if ext is None or ext[1] > IMAGE_READ:
                return s, None
            if ext[1] > len(buf):
                more = get(at + len(buf), ext[1] - len(buf))
                if more is None or len(more) < ext[1] - len(buf):
                    return s, None
                buf = buf + more
            return s, uncompressed(bytes(buf[ext[0]:ext[1]]), comp)
        return read_lines(want, one)

    def image_tracks(self):
        """Image subtitle tracks (PGS, VobSub) with their cue timings; forced/sign tracks marked."""
        from .imageref import IMAGE_CODECS
        out = []
        for t in self.tracks:
            if t.get("type") != 0x11 or not t.get("codec", "").startswith(IMAGE_CODECS):
                continue
            name = t.get("name", "").lower()
            out.append({"number": t["number"], "language": t.get("bcp47") or t.get("language", "und"),
                        "codec": t["codec"], "name": t.get("name", ""),
                        "forced": bool(t.get("forced")) or any(w in name for w in NOT_DIALOGUE),
                        "cues": self.track_cues.get(t["number"], [])})
        return out

    def audio_track_index(self):
        """Index among audio tracks (ffmpeg -map 0:a:N) of the default audio track, else the first."""
        audio = [t for t in self.tracks if t.get("type") == 2]
        for i, t in enumerate(audio):
            if t.get("default", 1):
                return i
        return 0 if audio else None

    def duration(self):
        if self.duration_s:
            return self.duration_s
        if self.cue_points:
            return max(t for t, _ in self.cue_points)
        return None

    def byte_range(self, start, length):
        end_t = start + length
        a = max((p for t, p in self.cue_points if t <= start), default=self.first_cluster)
        if a is None:
            raise NotMatroska("no cluster position")
        later = [p for t, p in self.cue_points if t >= end_t + 0.001 and p > a]
        b = min(later) if later else (self.cues_at if self.cues_at and self.cues_at > a else self.size)
        return a, b

    def audio_tracks(self):
        """Audio tracks in file order: {"index" (ffmpeg 0:a:N), "language", "default"}."""
        audio = [t for t in self.tracks if t.get("type") == 2]
        default = self.audio_track_index()
        return [{"index": i, "language": t.get("bcp47") or t.get("language", "und"), "default": i == default}
                for i, t in enumerate(audio)]

    def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg", chunk=256 * 1024, track=None):
        """16 kHz mono s16le PCM of [start, start+length) from the default audio track (or audio track `track`), one bounded read. The range runs
        to the first seek point after the window, which can be far past it on a file with sparse Cues: the read is
        streamed into the decoder and stops once the window is decoded. Bytes read are added to `audio_bytes`."""
        a, b = self.byte_range(start, length)
        header = self.ebml + bytes.fromhex("18538067") + bytes.fromhex("01FFFFFFFFFFFFFF") + self.info + self.tracks_raw
        cmd = [ffmpeg, "-nostdin", "-v", "error", "-f", "matroska", "-i", "pipe:0", "-ss", f"{start:.3f}",
               "-t", f"{length:.3f}", "-copyts", "-map", f"0:a:{track if track is not None else (self.audio_track_index() or 0)}",
               "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
        got = [0]

        def counted(chunks):   # this read's own bytes (other windows may be read at the same time)
            try:
                for c in chunks:
                    got[0] += len(c)
                    yield c
            finally:
                chunks.close()   # the decoder stopped early: the connection closes now
        try:
            return pipe_until(cmd, header, counted(self.r.stream(a, b - a, chunk)), int(length * sr) * 2)
        finally:
            add_audio_bytes(self, got[0])
