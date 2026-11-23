"""Matroska by range reads: head, tracks, the seek index (Cues), the OpenSubtitles hash and bounded audio windows.

mkvmerge writes one cue point per subtitle block (CueTime + CueDuration), so a text track's whole timing
comes from the Cues without reading any cluster. An audio window is read as the clusters that cover it,
planned from the Cues, prefixed with a minimal header (EBML + Segment of unknown size + Info + Tracks, no
attachments), and decoded from a pipe.
"""
import bisect
import logging
import math
import re
import struct
import subprocess
import threading
import zlib
from array import array
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import accumulate
from operator import itemgetter

from .imageref import IMAGE_CODECS
from .programs import limited
from .reader import CHUNK, IndexTooLarge, ReadFailed, Refused, Stopped

AUDIO_BYTES_LOCK = threading.Lock()
# ffmpeg decodes bytes of files anyone can publish; on Linux each decoder gets its own limits
FFMPEG_MEMORY_KB = 1024 * 1024    # address space of one decoder (1 GiB; a 30 s window needs a few MB)
FFMPEG_CPU_S = 120                # processor seconds of one decoder (a window takes well under one)
DECODE_WAIT_S = 300               # how long a decoder may still run once all its input is in; then it is stopped
DECODER_SAID = 1024               # bytes of a decoder's error output kept, logged when it fails short
# the only decoders ffmpeg may open, by its names for them (DTS's is dca, MP3's mp3float): those of the audio
# codecs Matroska and MP4 files carry. A window's header names every track of the file, and ffmpeg's probing would
# otherwise open each one's decoder (the video's too) on the window's bytes.
AUDIO_DECODERS = ("aac", "ac3", "eac3", "dca", "truehd", "mlp", "flac", "opus", "vorbis", "alac", "wavpack", "tta",
                  "mp3float", "mp3", "mp2float", "mp2", "mp1float", "mp1", "pcm_s16le", "pcm_s16be", "pcm_s24le",
                  "pcm_s24be", "pcm_s32le", "pcm_s32be", "pcm_f32le", "pcm_f64le", "pcm_u8")


def decoder_cmd(ffmpeg, fmt, args, linux=None):
    """An ffmpeg command that decodes audio from standard input in the format `fmt`: it opens only the pipe (no file
    or network address a file could name), only that one demuxer and only the decoders of AUDIO_DECODERS; on Linux
    within its memory and processor limits (programs.limited), past which ffmpeg dies."""
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "pipe", "-format_whitelist", fmt,
           "-codec_whitelist", ",".join(AUDIO_DECODERS), "-f", fmt, "-i", "pipe:0", *args]
    return limited(cmd, "ffmpeg", FFMPEG_MEMORY_KB, FFMPEG_CPU_S, linux)


def add_audio_bytes(index, n):
    """Bytes of audio windows read for this file (its audio budget); windows may be read in parallel."""
    with AUDIO_BYTES_LOCK:
        index.audio_bytes = getattr(index, "audio_bytes", 0) + n


HEAD_BYTES = 256 * 1024
HASH_CHUNK = 64 * 1024
ELEMENT_READ = 64 * 1024           # the first read of Info or Tracks found through the SeekHead (a few KB each)
MAX_ELEMENT = 8 * 1024 * 1024      # Info/Tracks larger than this are not read
MAX_CUES = 32 * 1024 * 1024        # 7.27 MB on a file with 75 PGS tracks; larger ones are never read (IndexTooLarge)
# cue track positions kept of one file (the 7.27 MB above hold a few hundred thousand); each is a few Python objects
# (a cue cluster and a subtitle cue at most), so a Cues of tiny made-up entries could otherwise hold a gigabyte: a file
# with more is not read (NotMatroska)
MAX_CUE_POINTS = 1_000_000
# track entries read of one file (real files have about a hundred at most: the 75 PGS tracks above); each is a dict
# kept for the job, and a first pass compares tracks pair by pair, so a Tracks of tiny made-up entries could otherwise
# hold half a gigabyte, or a worker for hours: a file with more is not read (NotMatroska)
MAX_TRACKS = 256
MAX_DURATION_S = 7 * 24 * 3600     # the longest runtime taken from a file; past it (or NaN, infinite, 0) it is unknown
# audio read before a window (an MP4's after it too), so that the decoder has primed and its first frame is whole when
# the window starts (a Matroska window read from a cluster that starts at its start began up to 96 ms late)
WINDOW_MARGIN_S = 0.1

# Matroska element ids (RFC 9559), by the specification's names: the top-level ones, then the children read
EBML, SEGMENT, SEEKHEAD, INFO, TRACKS, CUES, CLUSTER = (0x1A45DFA3, 0x18538067, 0x114D9B74, 0x1549A966,
                                                        0x1654AE6B, 0x1C53BB6B, 0x1F43B675)
# a CRC-32 or a Void may stand among any element's children (ffmpeg writes a CRC-32 first in each top-level one)
CRC32, VOID = 0xBF, 0xEC
SEEK, SEEK_ID, SEEK_POSITION = 0x4DBB, 0x53AB, 0x53AC
TIMESTAMP_SCALE, DURATION = 0x2AD7B1, 0x4489
TRACK_ENTRY, TRACK_NUMBER, TRACK_TYPE, CODEC_ID, CODEC_PRIVATE, NAME = 0xAE, 0xD7, 0x83, 0x86, 0x63A2, 0x536E
LANGUAGE, LANGUAGE_BCP47 = 0x22B59C, 0x22B59D
FLAG_DEFAULT, FLAG_FORCED, FLAG_HEARING_IMPAIRED, FLAG_VISUAL_IMPAIRED, FLAG_COMMENTARY = (0x88, 0x55AA, 0x55AB,
                                                                                          0x55AC, 0x55AF)
CONTENT_ENCODINGS, CONTENT_ENCODING, CONTENT_COMPRESSION = 0x6D80, 0x6240, 0x5034
CONTENT_COMP_ALGO, CONTENT_COMP_SETTINGS = 0x4254, 0x4255
CUE_POINT, CUE_TIME, CUE_TRACK_POSITIONS = 0xBB, 0xB3, 0xB7
CUE_TRACK, CUE_CLUSTER_POSITION, CUE_RELATIVE_POSITION, CUE_DURATION = 0xF7, 0xF1, 0xF0, 0xB2
SIMPLE_BLOCK, BLOCK_GROUP, BLOCK = 0xA3, 0xA0, 0xA1
TYPE_AUDIO, TYPE_SUBTITLE = 2, 0x11            # TrackType values
ZLIB, HEADER_STRIPPING = 0, 3                  # the ContentCompAlgo values undone (uncompressed); another is no answer
# a Segment's header with an 8-byte unknown size (all ones): an audio window's Segment is the rest of its input
UNKNOWN_SIZE_SEGMENT = SEGMENT.to_bytes(4, "big") + b"\x01" + b"\xff" * 7

TEXT_CODECS = ("S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/WEBVTT", "S_TEXT/ASCII")
# a track whose name holds one of these (whole words) is no dialogue: a commentary (the word in the main languages,
#) or an audio description, and of subtitle tracks a forced, signs or songs one; but not when its name also says
# it is the full track (FULL_TRACK: "Full Subtitles (Signs & Songs)")
COMMENTARY = ("commentary", "kommentar", "commentaire", "comentario", "commento", "commentaar", "komentarz",
              "комментарий", "audio description")
NOT_DIALOGUE = ("forced", "sign", "signs", "song", "songs", "karaoke") + COMMENTARY
FULL_TRACK = ("full", "dialogue", "complete")
NAME_WORD = re.compile(r"\w+")


WORD_READ = 2048                   # bytes read for one line's words (D1)
IMAGE_READ = 64 * 1024             # an image line's block, at most: 2 KB first, the rest only when needed
# what one line's zlib-compressed block may inflate to (deflate inflates up to about 1,000 times): a text line's words,
# and an image line's display set (an object as large as imagetext decodes, 4096 x 1024, is 4 MiB at a byte a pixel)
TEXT_INFLATE = 64 * 1024
IMAGE_INFLATE = 4 * 1024 * 1024
WORD_READS_AT_ONCE = 4
MAX_FAILED_IN_A_ROW = 3            # a track's line reads stop after this many failed reads in a row
STARTS_WITHIN_S = 0.0015           # a cue time this close to a line start asked for is that line's (starts_at)

log = logging.getLogger("subsync.read")


class NotMatroska(Exception):
    pass


# what a damaged (or made-up) field makes the parsers raise; open_index turns it into NotMatroska, so such a file is
# not readable as any other (the worker backs off) instead of being read again on every request
DAMAGED = (IndexError, struct.error, OverflowError, ZeroDivisionError, ValueError, RecursionError)


def named_not_dialogue(name, words=NOT_DIALOGUE):
    """Whether a track's name says it is no dialogue: one of `words` in it as whole words, and none of FULL_TRACK."""
    said = " " + " ".join(NAME_WORD.findall((name or "").lower())) + " "
    return any(f" {w} " in said for w in words) and not any(f" {w} " in said for w in FULL_TRACK)


def not_dialogue(track, words=NOT_DIALOGUE):
    """Whether a track is no dialogue: flagged a commentary or an audio description (Matroska's FlagCommentary,
    FlagVisualImpaired:), or so named (named_not_dialogue)."""
    return bool(track.get("commentary") or track.get("descriptive")) or named_not_dialogue(track.get("name"), words)


def sane_duration(d):
    """`d` when it can be a file's runtime in seconds (finite, over 0, at most MAX_DURATION_S), else None."""
    return d if d is not None and math.isfinite(d) and 0 < d <= MAX_DURATION_S else None


def index_hash(index):
    """The OpenSubtitles hash of an index's file. Its first and last 64 KiB are read only when the index doesn't hold
    them already: the head read holds the first, and the read of an index at the end of the file (Cues, moov) the
    last, as `last64`."""
    n = min(HASH_CHUNK, index.size)
    first = index.first64 if len(index.first64) >= n else index.r.get(0, HASH_CHUNK)
    last = index.last64 if len(index.last64) >= n else index.r.get(index.size - n, n)
    return oshash(index.size, first, last)


def starts_at(times, starts, within=STARTS_WITHIN_S):
    """[(start, k)] for each of `starts` that `times` (sorted) holds within `within` seconds, k its place in `times`."""
    found = []
    for s in starts:
        k = bisect.bisect_left(times, s - within)
        if k < len(times) and abs(times[k] - s) <= within:
            found.append((s, k))
    return found


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
        if eid not in (SIMPLE_BLOCK, BLOCK_GROUP):
            return None
        size, m = vint(buf, o + n)
        p = o + n + m
        if eid == BLOCK_GROUP:
            cid, n2 = vint(buf, p, strip=False)
            if cid != BLOCK:
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


def ass_text(block_text):
    """The text field of a Matroska ASS block (ReadOrder, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text)."""
    parts = block_text.split(",", 8)
    return parts[8] if len(parts) == 9 else block_text


def uncompressed(payload, comp, limit):
    """A block payload with the track's ContentCompression undone (zlib, or header stripping); None when it can't be,
    or when it would inflate past `limit` bytes (header stripping too, whose settings a file may make
    megabytes long). A zlib payload cut short (a text line's block past WORD_READ) gives what its start inflates to."""
    if payload is None or not comp:
        return payload
    if comp["algo"] == HEADER_STRIPPING:
        return comp["settings"] + payload if len(comp["settings"]) + len(payload) <= limit else None
    if comp["algo"] == ZLIB:
        d = zlib.decompressobj()
        try:
            out = d.decompress(payload, limit)
        except zlib.error:
            return None
        return None if d.unconsumed_tail else out
    return None


def read_lines(want, one, reads):
    """D1: `one(item)` -> (start, a line's text or block payload, or None) for each item, WORD_READS_AT_ONCE
    at once, each through `reads` (LineReads); {start: value}. A refused read or a stopped job ends every read not
    started yet and is raised. Once `reads` has stopped, the lines read so far are returned without waiting for reads
    still running: one the host never answers ends by itself at its timeout, and holds nothing up."""
    out = {}
    pool = ThreadPoolExecutor(max_workers=WORD_READS_AT_ONCE, thread_name_prefix="subsync-words")
    try:
        pending = {pool.submit(one, item) for item in want}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                s, value = fut.result()      # a refusal or a stopped job is raised as it is
                if value:
                    out[s] = value
            if reads.stop.is_set():
                if reads.error is not None:  # raised by a read whose own result isn't in yet
                    raise reads.error
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


class LineReads:
    """One track's line reads through `reader`, which stop for good once a read is refused or the job stops
    (both raised, and kept in `error`) or once MAX_FAILED_IN_A_ROW reads in a row have failed: a host that doesn't
    answer isn't asked for every line (a dead download address made 50 reads, each a call to the AIOStreams
    link). A read that works starts the count again, so a failure now and then stops nothing."""

    def __init__(self, reader):
        self.r = reader
        self.stop = threading.Event()
        self.error = None
        self._lock = threading.Lock()
        self._failed = 0

    def get(self, at, n):
        """reader.get(at, n), or None when it failed or the reads have stopped."""
        if self.stop.is_set():
            return None
        try:
            buf = self.r.get(at, n)
        except (Refused, Stopped) as e:
            self.error = e
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


def _fed(pipe, data):
    """Writes data to the decoder's input; False once the decoder has closed it (it ended, or stopped reading)."""
    try:
        pipe.write(data)
        return True
    except OSError:    # BrokenPipeError and the like: the decoder's doing, not the read's
        return False


def _read_some(pipe):
    return pipe.read1(65536) if hasattr(pipe, "read1") else pipe.read(65536)


def pipe_until(cmd, head, chunks, need, timeout=DECODE_WAIT_S):
    """Run a decoder on head + chunks (an iterator, e.g. RangeReader.stream) and stop feeding it once its output holds
    `need` bytes, or once it ends by itself; the iterator is then closed, which ends its read. Returns the output. An
    error of the iterator (the read failed, or was refused or stopped) is raised, once the decoder is stopped. A decoder
    that fails (exits with an error) short of `need` has the end of its error output logged: why it decoded
    too little (a codec it may not open, a -map that matches nothing, its limits)."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, said, enough = bytearray(), bytearray(), threading.Event()

    def drain():
        while b := _read_some(p.stdout):
            out.extend(b)
            if len(out) >= need:
                enough.set()
        enough.set()

    def listen():   # the decoder's error output: only its last DECODER_SAID bytes are kept
        while b := _read_some(p.stderr):
            said.extend(b)
            del said[:-DECODER_SAID]
    readers = [threading.Thread(target=f, daemon=True) for f in (drain, listen)]
    for t in readers:
        t.start()
    try:
        try:
            if _fed(p.stdin, head):
                for c in chunks:
                    if enough.is_set() or not _fed(p.stdin, c):
                        break
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
    except BaseException:   # the read failed: the decoder is stopped and waited for, not left decoding on its own
        p.kill()
        p.wait()
        raise
    finally:
        for t in readers:
            t.join(timeout)
        p.stdout.close()
        p.stderr.close()
    if p.returncode and len(out) < need:
        lines = [line.strip() for line in said.decode("utf-8", "replace").splitlines()]
        log.info("decoder failed (exit %s, %d of %d bytes): %s", p.returncode, len(out), need,
                 " | ".join(line for line in lines if line) or "-")
    return bytes(out)


def vint(buf, pos, strip=True):
    """(value, length) of the EBML variable-size integer at buf[pos] (an element id with strip=False). IndexError when
    it isn't wholly in buf: a field cut off at the buffer's end is never read as a shorter one."""
    if pos >= len(buf):
        raise IndexError("vint past the buffer")
    first = buf[pos]
    n = 1
    while n <= 8 and not (first & (0x80 >> (n - 1))):
        n += 1
    if n > 8:
        raise NotMatroska("bad vint")
    if pos + n > len(buf):
        raise IndexError("vint cut off at the buffer's end")
    val = first & ((0xFF >> n) if strip else 0xFF)
    for b in buf[pos + 1: pos + n]:
        val = (val << 8) | b
    return val, n


def elements(buf, pos, end, spill=False):
    """(id, element start, data start, data size) for each child element in buf[pos:end]. A child that doesn't fit
    there (nor in buf) ends the walk, as a field cut off at the buffer's end does: its size is damaged or made up, and
    read as it is, each such child would copy the rest of the buffer. With `spill` (read_head's walk of the
    Segment's top level) a child may run past `end`, the bytes held: read_head reads its rest, or stops there."""
    if not spill:
        end = min(end, len(buf))
    while pos < end:
        try:
            eid, n = vint(buf, pos, strip=False)
            size, m = vint(buf, pos + n)
        except IndexError:
            return
        data = pos + n + m
        if size == (1 << (7 * m)) - 1:  # unknown size: the rest of the parent
            size = end - data
        if not spill and (size < 0 or data + size > end):
            return
        yield eid, pos, data, size
        pos = data + size


def children(buf, pos, end, want):
    """(data start, data size) of each child element `want` in buf[pos:end] (elements). Any other child is passed over
    unread: a CRC-32 or a Void may stand among any element's children (ffmpeg writes a CRC-32 first in every
    top-level element, whose 4 bytes, read as entries, refused about one ffmpeg-made file in twenty)."""
    return ((p, size) for eid, _, p, size in elements(buf, pos, end) if eid == want)


def uint(b):
    """An EBML unsigned integer: at most 8 bytes; a longer one is damage (a TrackNumber of thousands of digits
    opened as fine, then failed every job that recorded it)."""
    if len(b) > 8:
        raise NotMatroska("an integer longer than 8 bytes")
    return int.from_bytes(b, "big") if b else 0


def utf8(b):
    """An EBML String or UTF-8 value: up to its first NUL, as the bytes from there on are padding (RFC 8794;: a
    zero-padded Language b"eng\\0" is English, not the unknown "eng\\0")."""
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


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
        self.cue_points = []     # the cue clusters (a property: set whole, with what byte_range bisects)
        self.track_cues = {}     # subtitle track number -> [(start s, duration s)]
        # subtitle track number -> (start times s, the byte where the line's block is looked for, -1 when the cue
        # doesn't say), in time order: compact arrays, as a file can have hundreds of thousands
        self.track_places = {}
        self.cues_at = None
        self.first_cluster = None
        self.ebml = self.info = self.tracks_raw = b""
        self.first64 = self.last64 = b""   # the file's first and last 64 KiB, when a read holds them (index_hash)
        self._head = b""                   # what the head read holds, from read_head to read_cues (_bytes)
        self.line_reads = {}               # subtitle track number -> its LineReads, one for the job (_line_reads)

    @property
    def cue_points(self):
        """[(time s, absolute position)] of the cue clusters in file order, a cluster's time its first cue's (any
        track's)."""
        return self._points

    @cue_points.setter
    def cue_points(self, points):
        """Set whole, never changed in place: kept in file order, and beside them their times in time order with the
        furthest cluster cued by each, which byte_range bisects (it walked every cue point three times a
        window, and a fit plans and reads dozens of windows)."""
        self._points = sorted(points, key=itemgetter(1))
        by_time = sorted(self._points)
        self._times = array("d", [t for t, _ in by_time])
        self._reach = array("q", accumulate((p for _, p in by_time), max))

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
        for eid, raw, p, sz in elements(head, self.seg_data, len(head), spill=True):
            if eid == CLUSTER:
                self.first_cluster = raw
                break
            if p + sz > len(head):
                if eid not in (SEEKHEAD, INFO, TRACKS) or p + sz - raw > MAX_ELEMENT:
                    break
                self._within_file(p + sz)
                head = head + self.r.get(len(head), p + sz - len(head))   # only the part not held yet
            self._element(head, eid, raw, p, sz)
        self._head = head
        for eid in (INFO, TRACKS):  # not in the first read: follow the SeekHead
            if eid in self.positions and not (self.info if eid == INFO else self.tracks_raw):
                self._fetch_element(self.positions[eid], eid)
        if not self.tracks:
            raise NotMatroska("no Tracks")
        return self

    def _bytes(self, at, n):
        """The file's bytes [at, at + n) (fewer at its end), read only when no read holds them already: the head, until
        read_cues, or the last 64 KiB. Bytes that reach the last 64 KiB are read on to the file's end, which the hash
        needs too (kept as `last64`): one request for both."""
        end = min(at + n, self.size)
        if at < len(self._head):   # the head holds them, or their start: only the rest is read
            held = self._head[at:end]
            return held if at + len(held) == end else held + self._bytes(len(self._head), end - len(self._head))
        tail_at = self.size - min(HASH_CHUNK, self.size)
        held = len(self.last64) == self.size - tail_at
        if held and at >= tail_at:
            return self.last64[at - tail_at:end - tail_at]
        if not held and end >= tail_at:
            start = min(at, tail_at)
            rest = self.r.get(start, self.size - start)
            if len(rest) == self.size - start:
                self.last64 = rest[tail_at - start:]
            return rest[at - start:end - start]
        return self.r.get(at, end - at)

    def _within_file(self, end):
        """An element the index reads that ends past the file's end (a file cut short, or made up) is damage,
        never read as a shorter one, nor read past the end (RangeReader refuses that as a failed read)."""
        if end > self.size:
            raise NotMatroska("an element runs past the file's end")

    def _fetch_element(self, at, want):
        """Info or Tracks (`want`) where the SeekHead puts them, in one read of ELEMENT_READ bytes and, for a longer
        one, a read of its rest (its header was read, then all of it again); one in the file's last 64 KiB
        comes with them (_bytes). A place at or past the file's end is not followed."""
        if at >= self.size:
            return
        buf = self._bytes(at, ELEMENT_READ)
        eid, n = vint(buf, 0, strip=False)
        sz, m = vint(buf, n)
        if eid != want or sz > MAX_ELEMENT:
            return
        self._within_file(at + n + m + sz)
        if n + m + sz > len(buf):
            buf += self._bytes(at + len(buf), n + m + sz - len(buf))
        self._element(buf, eid, 0, n + m, sz)

    def _element(self, buf, eid, raw, p, sz):
        if eid == SEEKHEAD:
            for sp, ssz in children(buf, p, p + sz, SEEK):
                sid = spos = None
                for cid, _, cp, csz in elements(buf, sp, sp + ssz):
                    if cid == SEEK_ID:
                        sid = uint(buf[cp:cp + csz])
                    elif cid == SEEK_POSITION:
                        spos = uint(buf[cp:cp + csz])
                if sid is not None and spos is not None:
                    self.positions.setdefault(sid, self.seg_data + spos)
        elif eid == INFO:
            self.info = bytes(buf[raw:p + sz])
            for cid, _, cp, csz in elements(buf, p, p + sz):
                if cid == TIMESTAMP_SCALE:
                    self.scale = uint(buf[cp:cp + csz])
                elif cid == DURATION and csz in (0, 4, 8):   # an EBML float; else damaged
                    self.duration_s = struct.unpack(">f" if csz == 4 else ">d", buf[cp:cp + csz])[0] if csz else 0.0
            if self.duration_s is not None:
                self.duration_s = self.duration_s * self.scale / 1e9
        elif eid == TRACKS:
            self.tracks_raw = bytes(buf[raw:p + sz])
            for k, (tp, tsz) in enumerate(children(buf, p, p + sz, TRACK_ENTRY), len(self.tracks) + 1):
                if k > MAX_TRACKS:   # counted as they are walked: an entry without a number costs its walk too
                    raise NotMatroska(f"more than {MAX_TRACKS} tracks")
                t = {"forced": 0, "default": 1, "language": "eng", "name": "", "codec": ""}
                for cid, _, cp, csz in elements(buf, tp, tp + tsz):
                    v = buf[cp:cp + csz]
                    if cid == TRACK_NUMBER:
                        t["number"] = uint(v)
                    elif cid == TRACK_TYPE:
                        t["type"] = uint(v)
                    elif cid == CODEC_ID:
                        t["codec"] = utf8(v)
                    elif cid == LANGUAGE:
                        t["language"] = utf8(v)
                        t["tagged"] = True        # a track without it is English by Matroska's rule, not by its tag
                    elif cid == LANGUAGE_BCP47:
                        t["bcp47"] = utf8(v)
                        t["tagged"] = True
                    elif cid == NAME:
                        t["name"] = utf8(v)
                    elif cid == FLAG_FORCED:
                        t["forced"] = uint(v)
                    elif cid == FLAG_COMMENTARY:
                        t["commentary"] = uint(v)
                    elif cid == FLAG_VISUAL_IMPAIRED:   # an audio description
                        t["descriptive"] = uint(v)
                    elif cid == FLAG_HEARING_IMPAIRED:  # SDH (pipeline.is_secondary)
                        t["hearing_impaired"] = uint(v)
                    elif cid == FLAG_DEFAULT:
                        t["default"] = uint(v)
                    elif cid == CODEC_PRIVATE:          # VobSub's palette (the .idx header), ASS's header
                        t["private"] = bytes(v)
                    elif cid == CONTENT_ENCODINGS:      # the compression of each block's payload
                        for ep, esz in children(buf, cp, cp + csz, CONTENT_ENCODING):
                            for kp, ksz in children(buf, ep, ep + esz, CONTENT_COMPRESSION):
                                comp = {"algo": ZLIB, "settings": b""}
                                for zid, _, zp, zsz in elements(buf, kp, kp + ksz):
                                    if zid == CONTENT_COMP_ALGO:
                                        comp["algo"] = uint(buf[zp:zp + zsz])
                                    elif zid == CONTENT_COMP_SETTINGS:
                                        comp["settings"] = bytes(buf[zp:zp + zsz])
                                t["compression"] = comp
                if t.get("number"):
                    self.tracks.append(t)

    def read_cues(self):
        """The seek index (Cues): each cue cluster's place and first cue time, and each subtitle track's cue times and
        block places. Cues the head read holds (at the front: mkclean, ffmpeg's reserve_index_space) are taken from it;
        Cues at the end of the file, where mkvmerge and ffmpeg write them, are read together with the file's last 64
        KiB, which the hash needs too (_bytes): one or two requests instead of three. The head is let go here."""
        try:
            at = self.positions.get(CUES)
            if not at or at >= self.size:   # none listed, or listed past the end of a file cut short: no Cues
                return self
            hdr = self._bytes(at, 16)
            try:
                eid, n = vint(hdr, 0, strip=False)
                csize, m = vint(hdr, n)
            except IndexError:              # the header is cut off at the file's end: no Cues
                return self
            if eid != CUES:
                return self
            if csize > MAX_CUES:   # ends as an index over the job's cap does, whatever that cap
                raise IndexTooLarge(MAX_CUES)
            body = self._bytes(at + n + m, csize)
            self.cues_at = at
            self._cue_points(body)
            return self
        finally:
            self._head = b""

    def _cue_points(self, body):
        subtitle = {t["number"] for t in self.tracks if t.get("type") == TYPE_SUBTITLE and t.get("number")}
        clusters, seen, kept = [], set(), 0
        for pp, psz in children(body, 0, len(body), CUE_POINT):
            t = None
            for cid, _, cp, csz in elements(body, pp, pp + psz):
                if cid == CUE_TIME:
                    t = uint(body[cp:cp + csz]) * self.scale / 1e9
                elif cid == CUE_TRACK_POSITIONS and t is not None:
                    kept += 1
                    if kept > MAX_CUE_POINTS:
                        raise NotMatroska(f"more than {MAX_CUE_POINTS} cue points")
                    trk = dur = cpos = rel = None
                    for gid, _, gp, gsz in elements(body, cp, cp + csz):
                        if gid == CUE_CLUSTER_POSITION and cpos is None:   # one each (repeats held 6x the cap)
                            cpos = self.seg_data + uint(body[gp:gp + gsz])
                            if cpos not in seen:
                                seen.add(cpos)
                                clusters.append((t, cpos))
                        elif gid == CUE_TRACK:
                            trk = uint(body[gp:gp + gsz])
                        elif gid == CUE_DURATION:
                            dur = uint(body[gp:gp + gsz]) * self.scale / 1e9
                        elif gid == CUE_RELATIVE_POSITION:
                            rel = uint(body[gp:gp + gsz])
                    if trk in subtitle:   # only subtitle tracks' cues are asked for (text_tracks, image_tracks)
                        self.track_cues.setdefault(trk, []).append((t, dur or 0.0))
                        # the Cluster's data starts 5 to 12 bytes after its position (block_extent looks at all eight)
                        place = -1 if cpos is None or rel is None else cpos + 5 + rel
                        times, places = self.track_places.setdefault(trk, (array("d"), array("q")))
                        times.append(t)
                        places.append(place if place < self.size else -1)
        self.cue_points = clusters
        for lst in self.track_cues.values():
            lst.sort()
        for times, places in self.track_places.values():
            if any(times[i] > times[i + 1] for i in range(len(times) - 1)):   # Cues are written in time order
                order = sorted(range(len(times)), key=times.__getitem__)
                times[:] = array("d", [times[i] for i in order])
                places[:] = array("q", [places[i] for i in order])

    def read_hash(self):
        return index_hash(self)

    # -- what the pipeline asks ---------------------------------------------------------------------------------
    def _subtitle_tracks(self, codecs):
        """The subtitle tracks with a codec in `codecs`, with their cue timings; tracks that are no dialogue (forced,
        signs, songs, commentary: not_dialogue) marked forced, and those flagged for the hearing impaired so marked."""
        out = []
        for t in self.tracks:
            if t.get("type") != TYPE_SUBTITLE or not t.get("codec", "").startswith(codecs):
                continue
            out.append({"number": t["number"], "language": t.get("bcp47") or t.get("language", "und"),
                        "codec": t["codec"], "name": t.get("name", ""),
                        "forced": bool(t.get("forced")) or not_dialogue(t),
                        "hearing_impaired": bool(t.get("hearing_impaired")),
                        "cues": self.track_cues.get(t["number"], [])})
        return out

    def text_tracks(self):
        """Text subtitle tracks with their cue timings; forced/sign/song tracks marked."""
        return self._subtitle_tracks(TEXT_CODECS)

    def line_words(self, track, starts):
        """D1: the text of the lines of text track `track` that start at `starts` (seconds), each read from its
        block's place in the Cues (one read of WORD_READ bytes; WORD_READS_AT_ONCE at once). Returns {start: text} for
        those it could read; a line whose cue has no place in its cluster, or whose block doesn't parse, is left out. A
        refused read (Refused) or a stopped job (Stopped) is raised; after MAX_FAILED_IN_A_ROW failed reads in a row no
        further line of the track is read, in this call or a later one (_line_reads)."""
        meta = next((t for t in self.tracks if t.get("number") == track), {})
        comp = meta.get("compression")
        want = self._places(track, starts)
        reads = self._line_reads(track)

        def one(item):
            s, at = item
            if self.size is not None and at >= self.size:
                return s, None
            buf = reads.get(at, WORD_READ)
            if buf is None:
                return s, None
            payload = uncompressed(block_payload(buf, track), comp, TEXT_INFLATE)
            if payload is None:
                return s, None
            text = payload.decode("utf-8", "replace")
            if text.count("\ufffd") > 0.1 * max(1, len(text)):
                return s, None
            if meta.get("codec", "").startswith(("S_TEXT/ASS", "S_TEXT/SSA")):
                text = ass_text(text)
            return s, text
        return read_lines(want, one, reads)

    def _line_reads(self, track):
        """The track's LineReads, one for the index's life (the job), so that its reads stop for good however many calls
        make them: pipeline.track_words reads an image track a batch of lines at a time, and a LineReads per
        call started the count of failed reads again in every batch (15 failed reads for 60 lines, not 3)."""
        return self.line_reads.setdefault(track, LineReads(self.r))

    def _places(self, track, starts):
        """[(start, the byte where its block's search begins)] for the starts whose cue has a place in its cluster."""
        times, places = self.track_places.get(track, ((), ()))
        return [(s, places[k]) for s, k in starts_at(times, starts) if places[k] >= 0]

    def codec_private(self, track):
        """A track's CodecPrivate (VobSub: the .idx header with its palette), b"" when it has none."""
        return next((t.get("private", b"") for t in self.tracks if t.get("number") == track), b"")

    def line_images(self, track, starts):
        """The block payloads (uncompressed) of the lines of image track `track` that start at `starts`: a first
        read of WORD_READ bytes, then the rest of the block when it is longer, never a block over IMAGE_READ bytes;
        WORD_READS_AT_ONCE lines at once. Returns {start: payload}. A refused read (Refused) or a stopped job (Stopped)
        is raised; after MAX_FAILED_IN_A_ROW failed reads in a row no further line of the track is read, in this call or
        a later one (_line_reads)."""
        meta = next((t for t in self.tracks if t.get("number") == track), {})
        comp = meta.get("compression")
        want = self._places(track, starts)
        reads = self._line_reads(track)

        def one(item):
            s, at = item
            if self.size is not None and at >= self.size:
                return s, None
            buf = reads.get(at, min(WORD_READ, IMAGE_READ))
            ext = block_extent(buf, track) if buf else None
            if ext is None or ext[1] > IMAGE_READ:
                return s, None
            if ext[1] > len(buf):
                more = reads.get(at + len(buf), ext[1] - len(buf))
                if more is None or len(more) < ext[1] - len(buf):
                    return s, None
                buf = buf + more
            return s, uncompressed(bytes(buf[ext[0]:ext[1]]), comp, IMAGE_INFLATE)
        return read_lines(want, one, reads)

    def image_tracks(self):
        """Image subtitle tracks (PGS, VobSub) with their cue timings; forced/sign tracks marked."""
        return self._subtitle_tracks(IMAGE_CODECS)

    def audio_track_index(self):
        """Index among audio tracks (ffmpeg -map 0:a:N) of the default audio track, else the first."""
        audio = [t for t in self.tracks if t.get("type") == TYPE_AUDIO]
        for i, t in enumerate(audio):
            if t.get("default", 1):
                return i
        return 0 if audio else None

    def duration(self):
        """The runtime in seconds: Info's Duration, else the last cue's time; None when neither is plausible."""
        return sane_duration(self.duration_s) or sane_duration(self._times[-1] if self._times else None)

    def byte_range(self, start, length):
        """The bytes that hold [start, start + length) and the WINDOW_MARGIN_S before it: from the last cue cluster at
        or before that margin's start (else the first cluster) to the cue cluster after the first one cued past the end
        (else to the Cues or the file's end). A cluster's time here is its first cue's, which can be later than the
        cluster's own start (mkvmerge cues every subtitle block, video only at keyframes), so that first one may still
        begin inside the window; the one after it can't, since the first one holds a block past the end."""
        points, place = self._points, itemgetter(1)
        k = bisect.bisect_right(self._times, start - WINDOW_MARGIN_S)
        if k:
            a = self._reach[k - 1]   # the furthest cluster cued by then
        else:           # nothing cued that early: the window starts with the file
            a = self.first_cluster if self.first_cluster is not None else (points[0][1] if points else None)
        if a is None:
            raise NotMatroska("no cluster position")
        b, end_t = None, start + length
        for j in range(bisect.bisect_right(points, a, key=place), len(points)):   # after `a`, in file order
            if points[j][0] >= end_t + 0.001:   # the first one cued past the end: to the one after it
                after = bisect.bisect_right(points, points[j][1], key=place)
                b = points[after][1] if after < len(points) else None
                break
        if b is None:
            b = self.cues_at if self.cues_at and self.cues_at > a else self.size
        return a, b

    def audio_tracks(self):
        """Audio tracks in file order: {"index" (ffmpeg 0:a:N), "language", "default", "name", "tagged" (whether the
        file names its language; untagged is English by Matroska's rule), "commentary", "descriptive" (the file's
        flags: a commentary, an audio description)}."""
        audio = [t for t in self.tracks if t.get("type") == TYPE_AUDIO]
        default = self.audio_track_index()
        return [{"index": i, "language": t.get("bcp47") or t.get("language", "und"), "default": i == default,
                 "name": t.get("name") or "", "tagged": bool(t.get("tagged")),
                 "commentary": bool(t.get("commentary")), "descriptive": bool(t.get("descriptive"))}
                for i, t in enumerate(audio)]

    def window_bytes(self, start, length):
        """What reading the audio window [start, start + length) really costs: the bytes of its byte_range."""
        a, b = self.byte_range(start, length)
        return b - a

    def window_pcm(self, start, length, sr, ffmpeg="ffmpeg", chunk=CHUNK, track=None):
        """Mono s16le PCM at `sr` Hz of [start, start+length) from the default audio track (or audio track `track`),
        one bounded read. The range runs to the first seek point after the window, which can be far past it on a file
        with sparse Cues: the read is streamed into the decoder and stops once the window is decoded. Bytes read are
        added to `audio_bytes`."""
        a, b = self.byte_range(start, length)
        header = self.ebml + UNKNOWN_SIZE_SEGMENT + self.info + self.tracks_raw
        cmd = decoder_cmd(ffmpeg, "matroska", [
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-copyts",
            "-map", f"0:a:{track if track is not None else (self.audio_track_index() or 0)}",
            "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"])
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
