"""Matroska by range reads: head, tracks, the seek index (Cues), the OpenSubtitles hash and bounded audio windows.

mkvmerge writes one cue point per subtitle block (CueTime + CueDuration), so a text track's whole timing
comes from the Cues without reading any cluster. An audio window is read as the clusters that cover it,
planned from the Cues, prefixed with a minimal header (EBML + Segment of unknown size + Info + Tracks, no
attachments), and decoded from a pipe.
"""
import struct
import subprocess

HEAD_BYTES = 256 * 1024
HASH_CHUNK = 64 * 1024
MAX_ELEMENT = 8 * 1024 * 1024      # Info/Tracks larger than this are not read
MAX_CUES = 32 * 1024 * 1024        # 7.27 MB on a file with 75 PGS tracks

EBML, SEGMENT, SEEKHEAD, INFO, TRACKS, CUES, CLUSTER = (0x1A45DFA3, 0x18538067, 0x114D9B74, 0x1549A966,
                                                        0x1654AE6B, 0x1C53BB6B, 0x1F43B675)
TEXT_CODECS = ("S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/WEBVTT", "S_TEXT/ASCII")
NOT_DIALOGUE = ("forced", "sign", "song", "karaoke", "commentary")


class NotMatroska(Exception):
    pass


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
        self.cues_at = None
        self.first_cluster = None
        self.ebml = self.info = self.tracks_raw = b""
        self.first64 = b""

    # -- reads -------------------------------------------------------------------------------------------------
    def read_head(self):
        head = self.r.get(0, HEAD_BYTES)
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
                    trk = dur = None
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
                    if trk is not None:
                        self.track_cues.setdefault(trk, []).append((t, dur or 0.0))
        self.cue_points.sort(key=lambda x: x[1])
        for lst in self.track_cues.values():
            lst.sort()
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

    def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg"):
        """16 kHz mono s16le PCM of [start, start+length) from the default audio track, one bounded read."""
        a, b = self.byte_range(start, length)
        data = self.r.get(a, b - a)
        header = self.ebml + bytes.fromhex("18538067") + bytes.fromhex("01FFFFFFFFFFFFFF") + self.info + self.tracks_raw
        out = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "matroska", "-i", "pipe:0", "-ss", f"{start:.3f}",
                              "-t", f"{length:.3f}", "-copyts", "-map", f"0:a:{self.audio_track_index() or 0}",
                              "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                             input=header + data, capture_output=True, timeout=300)
        return out.stdout
