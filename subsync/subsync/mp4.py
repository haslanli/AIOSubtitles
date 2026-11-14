"""MP4 (ISO base media) by range reads: the movie box, text subtitle timings, the OpenSubtitles hash and bounded
audio windows. The same questions MkvIndex answers, for .mp4/.m4v/.mov files.

Everything about timing lives in the `moov` box (at the start of a "fast start" file, else right after the media
data): a text track's lines are its samples, and each sample's start, duration and size are in the sample tables, so
the whole timing comes from `moov` without reading any media data. An empty sample (tx3g: 2 bytes, WebVTT: one empty
`vtte` box) is the gap between two lines. An audio window is the byte range of the audio samples that cover it; AAC
frames get an ADTS header each and AC-3/E-AC-3 frames are passed as they are, decoded from a pipe.
Fragmented files (`moof`) are not supported.
"""
import struct
import subprocess

import bisect

from .mkv import (HASH_CHUNK, HEAD_BYTES, NOT_DIALOGUE, WORD_READ, LineReads, NotMatroska, add_audio_bytes,
                  add_words_bytes, decoder_cmd, oshash, read_lines)

MAX_MOOV = 64 * 1024 * 1024
TEXT_SAMPLE_ENTRIES = {b"tx3g": 2, b"text": 2, b"wvtt": 8}   # codec -> largest size of an empty sample
AUDIO_RAW = {b"ac-3": "ac3", b"ec-3": "eac3"}
CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta")
ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)


class NotMp4(NotMatroska):
    """Not an MP4 this module can read (kept under NotMatroska so callers treat both the same way)."""


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
        self.first64 = b""
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
        if self.moov_size > MAX_MOOV:
            raise NotMp4("moov too large")
        end = self.moov_at + self.moov_size
        moov = self._head[self.moov_at:end] if end <= len(self._head) else self.r.get(self.moov_at, self.moov_size)
        self._head = b""
        for kind, p, e in boxes(moov, 0, len(moov)):
            if kind == b"moov":
                self._moov(moov, p, e)
        if not self.tracks:
            raise NotMp4("no tracks")
        return self

    def read_hash(self):
        last = self.r.get(max(0, self.size - HASH_CHUNK), min(HASH_CHUNK, self.size))
        first = self.first64 if len(self.first64) >= min(HASH_CHUNK, self.size) else self.r.get(0, HASH_CHUNK)
        return oshash(self.size, first, last)

    # -- moov --------------------------------------------------------------------------------------------------
    def _moov(self, b, p, e):
        for kind, q, f in boxes(b, p, e):
            if kind == b"mvhd":
                v = b[q]
                if v == 1:
                    self.movie_scale, dur = struct.unpack_from(">IQ", b, q + 20)
                else:
                    self.movie_scale, dur = struct.unpack_from(">II", b, q + 12)
                self.duration_s = dur / self.movie_scale if self.movie_scale else None
            elif kind == b"trak":
                t = self._trak(b, q, f)
                if t:
                    self.tracks.append(t)

    def _trak(self, b, p, e):
        t = {"id": None, "handler": b"", "codec": b"", "scale": 1, "lang": "und", "name": "", "stts": [], "sizes": [],
             "const": 0, "count": 0, "stsc": [], "chunks": [], "media_time": 0, "empty": 0, "enabled": True, "asc": b""}

        def walk(p, e):
            for kind, q, f in boxes(b, p, e):
                if kind in (b"mdia", b"minf", b"stbl", b"edts", b"udta"):
                    walk(q, f)
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
                elif kind == b"hdlr":
                    t["handler"] = b[q + 8:q + 12]
                    t["name"] = t["name"] or b[q + 24:f].split(b"\0")[0].decode("utf-8", "replace")
                elif kind == b"name":
                    t["name"] = b[q:f].decode("utf-8", "replace").strip("\0 ")
                elif kind == b"elst":
                    v, n = b[q], struct.unpack_from(">I", b, q + 4)[0]
                    at = q + 8
                    for _ in range(n):
                        if v == 1:
                            dur, mt = struct.unpack_from(">Qq", b, at)
                            at += 20
                        else:
                            dur, mt = struct.unpack_from(">Ii", b, at)
                            at += 12
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
                    n = struct.unpack_from(">I", b, q + 4)[0]
                    t["stts"] = list(struct.iter_unpack(">II", b[q + 8:q + 8 + 8 * n]))
                elif kind == b"stsz":
                    t["const"], t["count"] = struct.unpack_from(">II", b, q + 4)
                    if not t["const"]:
                        t["sizes"] = [x for (x,) in struct.iter_unpack(">I", b[q + 12:q + 12 + 4 * t["count"]])]
                elif kind == b"stsc":
                    n = struct.unpack_from(">I", b, q + 4)[0]
                    t["stsc"] = list(struct.iter_unpack(">III", b[q + 8:q + 8 + 12 * n]))
                elif kind == b"stco":
                    n = struct.unpack_from(">I", b, q + 4)[0]
                    t["chunks"] = [x for (x,) in struct.iter_unpack(">I", b[q + 8:q + 8 + 4 * n])]
                elif kind == b"co64":
                    n = struct.unpack_from(">I", b, q + 4)[0]
                    t["chunks"] = [x for (x,) in struct.iter_unpack(">Q", b[q + 8:q + 8 + 8 * n])]
        walk(p, e)
        return t if t["id"] is not None else None

    def _shift(self, t):
        """Seconds to add to a media time to get the presented time (edit list)."""
        return (t["empty"] / self.movie_scale if self.movie_scale else 0.0) - t["media_time"] / t["scale"]

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
        times = [x[0] for x in lines]
        offsets = self._sample_offsets(t)
        want = []
        for s in starts:
            k = bisect.bisect_left(times, s - 0.0015)
            if k < len(lines) and abs(lines[k][0] - s) <= 0.0015 and lines[k][2] < len(offsets):
                i = lines[k][2]
                want.append((s, offsets[i], min(self._size(t, i), WORD_READ)))

        reads = LineReads(self.r)

        def one(item):
            s, at, size = item
            data = reads.get(at, size)
            if data is None:
                return s, None
            add_words_bytes(self, len(data))
            return s, sample_text(t["codec"], data)
        return read_lines(want, one)

    # -- what the pipeline asks ---------------------------------------------------------------------------------
    def text_tracks(self):
        out = []
        for t in self.tracks:
            if t["codec"] not in TEXT_SAMPLE_ENTRIES or t["handler"] not in (b"sbtl", b"text", b"subt"):
                continue
            name = t["name"].lower()
            out.append({"number": t["id"], "language": t["lang"], "codec": "mp4/" + t["codec"].decode("ascii", "replace"),
                        "name": t["name"], "forced": any(w in name for w in NOT_DIALOGUE), "cues": self._lines(t)})
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

    def audio_track_index(self):
        t = self._audio()
        return None if t is None else [x for x in self.tracks if x["handler"] == b"soun"].index(t)

    def duration(self):
        return self.duration_s

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

    def byte_range(self, start, length):
        t = self._audio()
        if t is None:
            raise NotMp4("no audio this module can decode")
        first, last, _ = self._samples_in(t, start, start + length)
        offs = self._sample_offsets(t)
        if first is None or last >= len(offs):
            raise NotMp4("no audio samples there")
        return offs[first], offs[last] + self._size(t, last)

    def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg"):
        """16 kHz mono s16le PCM of [start, start+length) from the first usable audio track, one bounded read."""
        t = self._audio()
        if t is None:
            raise NotMp4("no audio this module can decode")
        first, last, first_time = self._samples_in(t, start - 0.1, start + length + 0.1)
        offs = self._sample_offsets(t)
        if first is None or last >= len(offs):
            raise NotMp4("no audio samples there")
        a, b = offs[first], offs[last] + self._size(t, last)
        data = self.r.get(a, b - a)
        add_audio_bytes(self, len(data))
        if t["codec"] == b"mp4a":
            fmt, parts = "aac", []
            for i in range(first, last + 1):
                frame = data[offs[i] - a: offs[i] - a + self._size(t, i)]
                parts.append(_adts(t["asc"], len(frame)) + frame)
            stream = b"".join(parts)
        else:
            fmt = AUDIO_RAW[t["codec"]]
            stream = b"".join(data[offs[i] - a: offs[i] - a + self._size(t, i)] for i in range(first, last + 1))
        skip = max(0.0, start - first_time)
        out = subprocess.run(decoder_cmd(ffmpeg, fmt, ["-ss", f"{skip:.3f}", "-t", f"{length:.3f}", "-ac", "1",
                                                       "-ar", str(sr), "-f", "s16le", "-"]),
                             input=stream, capture_output=True, timeout=300)
        return out.stdout


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
        p += 3 + (2 if flags & 0x80 else 0) + (2 if flags & 0x20 else 0)
        if flags & 0x40:
            p += 1 + entry[p]
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
    """The index for a file, chosen by its first bytes: Matroska, else MP4. Raises NotMatroska for anything else."""
    from .mkv import EBML, MkvIndex, vint
    head = reader.get(0, HEAD_BYTES)
    try:
        is_mkv = vint(head, 0, strip=False)[0] == EBML
    except NotMatroska:
        is_mkv = False
    if is_mkv:
        return MkvIndex(reader).read_head(head).read_cues()
    if looks_like_mp4(head):
        return Mp4Index(reader).read_head(head).read_cues()
    raise NotMatroska("neither Matroska nor MP4")
