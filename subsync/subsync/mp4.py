"""MP4 (ISO base media) by range reads: the movie box, text subtitle timings, the OpenSubtitles hash and bounded
audio windows. The same questions MkvIndex answers, for .mp4/.m4v/.mov files.

Everything about timing lives in the `moov` box (at the start of a "fast start" file, else right after the media
data): a text track's lines are its samples, and each sample's start, duration and size are in the sample tables, so
the whole timing comes from `moov` without reading any media data. An empty sample (tx3g: 2 bytes, WebVTT: one empty
`vtte` box) is the gap between two lines. An audio window is the byte range of the audio samples that cover it, streamed:
each sample is cut out as its bytes pass, AAC frames with an ADTS header each and AC-3/E-AC-3 frames as they are, and
fed to the decoder through a pipe.
Fragmented files (an `mvex` box in the movie box, or `moof`) are not supported: they are read as no MP4.
"""
import struct

from .mkv import (DAMAGED, EBML, HASH_CHUNK, HEAD_BYTES, WINDOW_MARGIN_S, WORD_READ, LineReads, MkvIndex, NotMatroska,
                  add_audio_bytes, decoder_cmd, index_hash, named_not_dialogue, pipe_until, read_lines, sane_duration,
                  starts_at, vint)
from .reader import IndexTooLarge

MAX_MOOV = 64 * 1024 * 1024      # a larger movie box is never read (IndexTooLarge)
# the samples of a track walked one by one (_lines, _sample_offsets) cost a Python object or more each, while a few bytes
# of the movie box can claim billions: a file with more than these is not read (NotMp4)
MAX_TEXT_SAMPLES = 1_000_000     # of all its text tracks together (a film's track: a few thousand lines and gaps)
MAX_AUDIO_SAMPLES = 5_000_000    # of the audio track its windows are cut from (3 hours of 48 kHz AAC: about 0.5 M)
# how many times its share of the file an audio window's bytes may span: a real file's busiest scene holds a few times
# its share, a made-up chunk table can place a window's last sample gigabytes away (NotMp4)
MAX_WINDOW_SPAN = 8
TEXT_SAMPLE_ENTRIES = {b"tx3g": 2, b"text": 2, b"wvtt": 8}   # codec -> largest size of an empty sample
AUDIO_RAW = {b"ac-3": "ac3", b"ec-3": "eac3"}
TRAK_CONTAINERS = (b"mdia", b"minf", b"stbl", b"edts", b"udta")   # the boxes in a trak that hold the boxes read
ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)


class NotMp4(NotMatroska):
    """Not an MP4 this module can read (kept under NotMatroska so callers treat both the same way)."""


class NoAudioThere(NotMp4):
    """No sample of the audio track covers the asked time (an audio window past the end of the file's audio)."""


def boxes(buf, pos, end):
    """(type, payload start, box end) for each box in buf[pos:end]; stops at a box that does not fit."""
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, pos)
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        yield kind, pos + head, pos + size
        pos += size


def _table(b, q, f, fmt, count_at=4):
    """The entries of a sample table box whose payload is b[q:f]: its 32-bit entry count at q + count_at, the entries,
    each `fmt`, right after it. NotMp4 when the count claims more entries than the box holds."""
    n = struct.unpack_from(">I", b, q + count_at)[0]
    at, size = q + count_at + 4, struct.calcsize(fmt)
    if n * size > f - at:
        raise NotMp4("table larger than its box")
    return list(struct.iter_unpack(fmt, b[at:at + n * size]))


def looks_like_mp4(head):
    return len(head) >= 12 and head[4:8] in (b"ftyp", b"moov", b"free", b"skip", b"wide", b"mdat", b"styp")


class Mp4Index:
    """What subsync needs from one MP4 file, read through a RangeReader."""

    def __init__(self, reader):
        self.r = reader
        self.size = None
        self.duration_s = None
        self.movie_scale = 1000
        self.tracks = []
        self.first64 = self.last64 = b""   # the file's first and last 64 KiB, when a read holds them (index_hash)
        self.moov_at = self.moov_size = None
        self._head = b""
        self._offsets = {}

    # -- reads -------------------------------------------------------------------------------------------------
    def read_head(self, head=None):
        head = self.r.get(0, HEAD_BYTES) if head is None else head
        self.size = self.r.size
        if self.size is None:
            raise NotMp4("no size")
        self.first64 = head[:HASH_CHUNK]
        self._head = head
        if not looks_like_mp4(head):
            raise NotMp4("not MP4")
        pos = 0
        for _ in range(64):
            if pos + 16 > self.size:
                break
            hdr = head[pos:pos + 16] if pos + 16 <= len(head) else self.r.get(pos, 16)
            size, kind = struct.unpack_from(">I4s", hdr, 0)
            if size == 1:
                size = struct.unpack_from(">Q", hdr, 8)[0]
            elif size == 0:
                size = self.size - pos
            if size < 8:
                break
            if kind == b"moov":
                self.moov_at, self.moov_size = pos, size
                return self
            if kind == b"moof":
                raise NotMp4("fragmented MP4")
            pos += size
        raise NotMp4("no moov")

    def read_cues(self):
        """The movie box: what the head read holds of it, and the rest read once; a movie box that ends the file is read
        with the file's last 64 KiB, which the hash needs too (kept as `last64`). A file whose tracks claim more samples
        than MAX_TEXT_SAMPLES or MAX_AUDIO_SAMPLES is not read; without a usable movie length (the header's all ones
        say "unknown") the runtime is where the audio track ends."""
        if self.moov_size > MAX_MOOV:   # ends as an index over the job's cap does, whatever that cap
            raise IndexTooLarge(MAX_MOOV)
        end = min(self.moov_at + self.moov_size, self.size)
        moov = self._head[self.moov_at:end]           # what the head read holds of it: all of it in a fast-start file
        start = self.moov_at + len(moov)              # its first byte not held
        if start < end and end == self.size:          # it ends the file: one read gives the rest and the hash's tail
            n = min(HASH_CHUNK, self.size)
            at = min(start, self.size - n)
            tail = self.r.get(at, self.size - at)
            moov += tail[start - at:]
            self.last64 = tail[len(tail) - n:]
        elif start < end:
            moov += self.r.get(start, end - start)
        self._head = b""
        for kind, p, e in boxes(moov, 0, len(moov)):
            if kind == b"moov":
                self._moov(moov, p, e)
        if not self.tracks:
            raise NotMp4("no tracks")
        audio = self._audio()
        if sum(t["count"] for t in self.tracks if t["codec"] in TEXT_SAMPLE_ENTRIES) > MAX_TEXT_SAMPLES \
                or (audio is not None and audio["count"] > MAX_AUDIO_SAMPLES):
            raise NotMp4("too many samples")
        if sane_duration(self.duration_s) is None and audio is not None:   # no usable length: the audio track's own
            self.duration_s = self._end(audio)
        return self

    def read_hash(self):
        return index_hash(self)

    # -- moov --------------------------------------------------------------------------------------------------
    def _moov(self, b, p, e):
        for kind, q, f in boxes(b, p, e):
            if kind == b"mvhd":
                v = b[q]
                if v == 1:
                    self.movie_scale, dur = struct.unpack_from(">IQ", b, q + 20)
                else:
                    self.movie_scale, dur = struct.unpack_from(">II", b, q + 12)
                unknown = dur == (1 << (64 if v == 1 else 32)) - 1     # all ones: "can't be determined" (ISO 14496-12)
                self.duration_s = dur / self.movie_scale if self.movie_scale and not unknown else None
            elif kind == b"trak":
                t = self._trak(b, q, f)
                if t:
                    self.tracks.append(t)
            elif kind == b"mvex":   # the mark of a fragmented file: its samples are in moof boxes, not read here
                raise NotMp4("fragmented MP4")

    def _trak(self, b, p, e):
        t = {"id": None, "handler": b"", "codec": b"", "scale": 1, "lang": "und", "name": "", "stts": [], "sizes": [],
             "const": 0, "count": 0, "stsc": [], "chunks": [], "media_time": 0, "empty": 0, "enabled": True, "asc": b""}

        def walk(p, e, parent=b"trak"):
            for kind, q, f in boxes(b, p, e):
                if kind in TRAK_CONTAINERS:
                    walk(q, f, kind)
                elif kind == b"tkhd":
                    v, flags = b[q], int.from_bytes(b[q + 1:q + 4], "big")
                    t["enabled"] = bool(flags & 1)
                    t["id"] = struct.unpack_from(">I", b, q + (20 if v == 1 else 12))[0]
                elif kind == b"mdhd":
                    v = b[q]
                    if v == 1:
                        t["scale"] = struct.unpack_from(">I", b, q + 20)[0]
                        lang = struct.unpack_from(">H", b, q + 32)[0]
                    else:
                        t["scale"] = struct.unpack_from(">I", b, q + 12)[0]
                        lang = struct.unpack_from(">H", b, q + 20)[0]
                    c = [(lang >> s) & 31 for s in (10, 5, 0)]
                    if all(1 <= x <= 26 for x in c):
                        t["lang"] = "".join(chr(x + 0x60) for x in c)
                elif kind == b"hdlr" and parent == b"mdia":
                    # the media handler only: a QuickTime file's minf holds a data handler too, which says nothing of
                    # the track's kind
                    t["handler"] = b[q + 8:q + 12]
                    t["name"] = t["name"] or _handler_name(b[q + 24:f], quicktime=b[q + 4:q + 8] != bytes(4))
                elif kind == b"name":
                    t["name"] = b[q:f].decode("utf-8", "replace").strip("\0 ")
                elif kind == b"elst":   # each edit: its duration, media time and rate
                    for dur, mt, _ in _table(b, q, f, ">QqI" if b[q] == 1 else ">IiI"):
                        if mt == -1:
                            t["empty"] += dur          # an empty edit: the track starts later (movie timescale)
                        else:
                            t["media_time"] = mt       # the first real edit: media before it is not presented
                            break
                elif kind == b"stsd":
                    if struct.unpack_from(">I", b, q + 4)[0] >= 1 and q + 16 <= f:
                        esize, t["codec"] = struct.unpack_from(">I4s", b, q + 8)
                        if t["codec"] == b"mp4a":
                            t["asc"] = _audio_specific_config(b[q + 8:min(f, q + 8 + esize)])
                elif kind == b"stts":
                    t["stts"] = _table(b, q, f, ">II")
                elif kind == b"stsz":
                    t["const"], t["count"] = struct.unpack_from(">II", b, q + 4)
                    if not t["const"]:
                        t["sizes"] = [x for (x,) in _table(b, q, f, ">I", count_at=8)]
                elif kind == b"stsc":
                    t["stsc"] = _table(b, q, f, ">III")
                elif kind == b"stco":
                    t["chunks"] = [x for (x,) in _table(b, q, f, ">I")]
                elif kind == b"co64":
                    t["chunks"] = [x for (x,) in _table(b, q, f, ">Q")]
        walk(p, e)
        return t if t["id"] is not None and t["scale"] else None   # a timescale of 0 can't time anything: left out

    def _shift(self, t):
        """Seconds to add to a media time to get the presented time (edit list)."""
        return (t["empty"] / self.movie_scale if self.movie_scale else 0.0) - t["media_time"] / t["scale"]

    def _end(self, t):
        """The presented time at which the track's last sample ends."""
        return sum(count * delta for count, delta in t["stts"]) / t["scale"] + self._shift(t)

    def _size(self, t, i):
        return t["const"] or t["sizes"][i]

    def _lines(self, t, samples=False):
        """The text track's lines as (start, duration); with samples, (start, duration, sample index)."""
        empty = TEXT_SAMPLE_ENTRIES[t["codec"]]
        shift, scale, out, at, i = self._shift(t), t["scale"], [], 0, 0
        n = t["count"]
        for count, delta in t["stts"]:
            for _ in range(count):
                if i >= n:
                    break
                if self._size(t, i) > empty and delta > 0:
                    out.append((at / scale + shift, delta / scale, i) if samples else (at / scale + shift, delta / scale))
                at += delta
                i += 1
        return out

    def line_words(self, track, starts):
        """D1: the text of the lines of text track `track` that start at `starts`, each sample read where the
        sample tables put it (at most WORD_READ bytes; WORD_READS_AT_ONCE at once). Returns {start: text}; reads stop as
        mkv.LineReads says."""
        t = next((x for x in self.tracks if x["id"] == track and x["codec"] in TEXT_SAMPLE_ENTRIES), None)
        if t is None:
            return {}
        lines = self._lines(t, samples=True)
        offsets = self._sample_offsets(t)
        want = []
        for s, k in starts_at([x[0] for x in lines], starts):
            i = lines[k][2]
            if i < len(offsets):
                want.append((s, offsets[i], min(self._size(t, i), WORD_READ)))

        reads = LineReads(self.r)

        def one(item):
            s, at, size = item
            data = reads.get(at, size)
            if data is None:
                return s, None
            return s, sample_text(t["codec"], data)
        return read_lines(want, one, reads)

    # -- what the pipeline asks ---------------------------------------------------------------------------------
    def text_tracks(self):
        out = []
        for t in self.tracks:
            if t["codec"] not in TEXT_SAMPLE_ENTRIES or t["handler"] not in (b"sbtl", b"text", b"subt"):
                continue
            out.append({"number": t["id"], "language": t["lang"], "codec": "mp4/" + t["codec"].decode("ascii", "replace"),
                        "name": t["name"], "forced": named_not_dialogue(t["name"]), "cues": self._lines(t)})
        return out

    def image_tracks(self):
        return []

    def _audio(self):
        audio = [t for t in self.tracks if t["handler"] == b"soun"]
        usable = [t for t in audio if (t["codec"] == b"mp4a" and t["asc"]) or t["codec"] in AUDIO_RAW]
        for t in usable:
            if t["enabled"]:
                return t
        return usable[0] if usable else None

    def duration(self):
        return sane_duration(self.duration_s)

    def _sample_offsets(self, t):
        if t["id"] not in self._offsets:
            offs, i, n = [], 0, t["count"]
            runs = t["stsc"]
            for k, (first, per, _) in enumerate(runs):
                last = runs[k + 1][0] - 1 if k + 1 < len(runs) else len(t["chunks"])
                for chunk in range(first, last + 1):
                    at = t["chunks"][chunk - 1]
                    for _ in range(per):
                        if i >= n:
                            break
                        offs.append(at)
                        at += self._size(t, i)
                        i += 1
            self._offsets[t["id"]] = offs
        return self._offsets[t["id"]]

    def _samples_in(self, t, start, end):
        """(first index, last index, presented time of the first) of the samples that cover [start, end)."""
        shift, scale, at, i = self._shift(t), t["scale"], 0, 0
        first = first_time = last = None
        for count, delta in t["stts"]:
            if delta <= 0:
                i += count
                continue
            a, z = at / scale + shift, (at + count * delta) / scale + shift
            if z > start and a < end:
                lo = max(0, int((start - a) * scale // delta))
                hi = min(count - 1, int((end - a) * scale // delta))
                if first is None:
                    first, first_time = i + lo, a + lo * delta / scale
                last = i + hi
            at += count * delta
            i += count
        return first, last, first_time

    def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg", chunk=256 * 1024):
        """16 kHz mono s16le PCM of [start, start+length) from the first usable audio track. The byte range of the
        samples that cover it (WINDOW_MARGIN_S more on each side) is one bounded read, streamed as MkvIndex's is: each
        sample is cut out as its bytes pass and fed to the decoder, and the read stops once the window is decoded, so an
        interleaved file's video is never held. A range wider than MAX_WINDOW_SPAN times the window's share of the file
        is not read (NotMp4). Bytes read are added to `audio_bytes`."""
        t = self._audio()
        if t is None:
            raise NotMp4("no audio this module can decode")
        first, last, first_time = self._samples_in(t, start - WINDOW_MARGIN_S, start + length + WINDOW_MARGIN_S)
        offs = self._sample_offsets(t)
        if first is None or last >= len(offs):
            raise NoAudioThere("no audio samples there")
        order = sorted(range(first, last + 1), key=offs.__getitem__)   # the samples in file order (as muxers store them)
        a, b = offs[order[0]], max(offs[i] + self._size(t, i) for i in order)
        d = self.duration()
        if d and b - a > MAX_WINDOW_SPAN * (length + 2 * WINDOW_MARGIN_S) * self.size / d:
            raise NotMp4("audio samples spread far past their window")
        fmt = "aac" if t["codec"] == b"mp4a" else AUDIO_RAW[t["codec"]]
        skip = max(0.0, start - first_time)
        cmd = decoder_cmd(ffmpeg, fmt, ["-ss", f"{skip:.3f}", "-t", f"{length:.3f}", "-ac", "1", "-ar", str(sr),
                                        "-f", "s16le", "-"])
        got = [0]

        def samples(chunks):
            """The window's samples in decoding order, each cut out of the read once all its bytes are in (those a
            piece of the read completes, joined); only the bytes a sample still needs are kept."""
            buf, at, k, cut, nxt = bytearray(), a, 0, {}, first   # buf: the file's bytes from `at` on
            try:
                for c in chunks:
                    got[0] += len(c)
                    buf += c
                    while k < len(order) and offs[order[k]] + self._size(t, order[k]) <= at + len(buf):
                        i = order[k]
                        cut[i] = bytes(buf[offs[i] - at:offs[i] - at + self._size(t, i)])
                        k += 1
                    done = min(offs[order[k]] - at, len(buf)) if k < len(order) else len(buf)
                    del buf[:done]
                    at += done
                    out = []
                    while nxt in cut:
                        frame = cut.pop(nxt)
                        out.append(_adts(t["asc"], len(frame)) + frame if fmt == "aac" else frame)
                        nxt += 1
                    if out:
                        yield b"".join(out)
            finally:
                chunks.close()   # the decoder stopped early: the connection closes now
        try:
            return pipe_until(cmd, b"", samples(self.r.stream(a, b - a, chunk)), int(length * sr) * 2)
        finally:
            add_audio_bytes(self, got[0])


def _handler_name(raw, quicktime):
    """A handler box's name: in a QuickTime file (the box names its component type) a counted string, its first byte
    the length; else a string ended by a zero."""
    if quicktime and raw and raw[0] < len(raw):
        return raw[1:1 + raw[0]].decode("utf-8", "replace")
    return raw.split(b"\0")[0].decode("utf-8", "replace")


def sample_text(codec, data):
    """A text sample's text: tx3g/text a 16-bit length then UTF-8; WebVTT (wvtt) the `payl` boxes of its `vttc` cues."""
    if codec in (b"tx3g", b"text"):
        if len(data) < 2:
            return None
        n = struct.unpack_from(">H", data, 0)[0]
        return data[2:2 + n].decode("utf-8", "replace")
    if codec == b"wvtt":
        out = []
        for kind, q, f in boxes(data, 0, len(data)):
            if kind == b"vttc":
                for k2, q2, f2 in boxes(data, q, f):
                    if k2 == b"payl":
                        out.append(data[q2:f2].decode("utf-8", "replace"))
        return "\n".join(out) or None
    return None


def _audio_specific_config(entry):
    """(object type, sample rate index, channels) from an mp4a sample entry's esds box, or b"" when not found."""
    at = entry.find(b"esds")
    if at < 0:
        return b""
    p = at + 8                     # past the type and the version/flags
    end = len(entry)

    def descriptor(p):
        tag = entry[p]
        p += 1
        size = 0
        for _ in range(4):
            c = entry[p]
            p += 1
            size = (size << 7) | (c & 0x7F)
            if not c & 0x80:
                break
        return tag, p, size
    try:
        tag, p, _ = descriptor(p)
        if tag != 0x03:
            return b""
        flags = entry[p + 2]
        p += 3                       # ES_ID, flags; then, in this order (ISO 14496-1), the fields the flags announce:
        if flags & 0x80:
            p += 2                   # dependsOn_ES_ID
        if flags & 0x40:
            p += 1 + entry[p]        # URL length and URL
        if flags & 0x20:
            p += 2                   # OCR_ES_Id
        tag, p, _ = descriptor(p)
        if tag != 0x04:
            return b""
        p += 13
        tag, p, size = descriptor(p)
        if tag != 0x05 or size < 2 or p + 2 > end:
            return b""
        bits = int.from_bytes(entry[p:p + 2], "big")
        obj, rate, chans = bits >> 11, (bits >> 7) & 15, (bits >> 3) & 15
        if obj in (5, 29):         # SBR/PS signalled explicitly: the core is AAC LC at this rate
            obj = 2
        if not 1 <= obj <= 4 or rate >= len(ADTS_RATES) or not 1 <= chans <= 7:
            return b""
        return bytes((obj, rate, chans))
    except IndexError:
        return b""


def _adts(asc, frame_len):
    obj, rate, chans = asc
    n = frame_len + 7
    return bytes((0xFF, 0xF1, ((obj - 1) << 6) | (rate << 2) | (chans >> 2), ((chans & 3) << 6) | (n >> 11),
                  (n >> 3) & 0xFF, ((n & 7) << 5) | 0x1F, 0xFC))


def open_index(reader):
    """The index for a file, chosen by its first bytes: Matroska, else MP4. Raises NotMatroska for anything else, and for
    a file whose index is damaged (mkv.DAMAGED)."""
    head = reader.get(0, HEAD_BYTES)
    try:
        is_mkv = vint(head, 0, strip=False)[0] == EBML
    except (NotMatroska, IndexError):   # no EBML id there, or not even its bytes
        is_mkv = False
    try:
        if is_mkv:
            return MkvIndex(reader).read_head(head).read_cues()
        if looks_like_mp4(head):
            return Mp4Index(reader).read_head(head).read_cues()
    except DAMAGED as e:
        raise NotMatroska(f"damaged index ({str(e) or type(e).__name__})") from None
    raise NotMatroska("neither Matroska nor MP4")
