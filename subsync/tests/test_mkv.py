"""The file index on damaged or made-up files, and how it reads them: blocks that inflate only so far, line reads that
stop without waiting for a read that hangs, the decoder's pipe on a failed read, fields cut off at a buffer's end, the
Cues read with the hash's last 64 KiB, cue points kept compactly and at most MAX_CUE_POINTS, a plausible runtime, and
the clusters an audio window needs. No network: files are served from memory."""
import math
import struct
import subprocess
import sys
import threading
import time
import tracemalloc
import unittest
import zlib
from array import array
from unittest import mock

from helpers import el, fixture, have, matroska, Memory, Opener, text_track  # noqa: E402
from subsync import mkv  # noqa: E402
from subsync.mkv import HASH_CHUNK, HEAD_BYTES, MkvIndex, NotMatroska, oshash  # noqa: E402
from subsync.mp4 import Mp4Index, open_index  # noqa: E402
from subsync.reader import CapReached, IndexTooLarge, RangeReader, ReadFailed  # noqa: E402
from subsync.worker import audio_facts  # noqa: E402


class InflateLimitTest(unittest.TestCase):
    """A line's zlib-compressed block (ContentCompression) inflates at most to its limit; a few KB of zlib can
    inflate a thousandfold."""

    ZLIB = {"algo": 0, "settings": b""}

    def test_a_block_that_inflates_past_its_limit_is_no_answer(self):
        bomb = zlib.compress(bytes(10_000_000), 9)
        self.assertLess(len(bomb), mkv.IMAGE_READ)
        self.assertIsNone(mkv.uncompressed(bomb, self.ZLIB, mkv.IMAGE_INFLATE))
        self.assertIsNone(mkv.uncompressed(bomb[:mkv.WORD_READ], self.ZLIB, mkv.TEXT_INFLATE))

    def test_an_ordinary_block_and_one_cut_short_still_inflate(self):
        text = "".join(f"Line {i} of the film, said out loud. " for i in range(40)).encode()
        packed = zlib.compress(text)
        self.assertEqual(mkv.uncompressed(packed, self.ZLIB, mkv.TEXT_INFLATE), text)
        start = mkv.uncompressed(packed[:len(packed) // 2], self.ZLIB, mkv.TEXT_INFLATE)
        self.assertTrue(start and text.startswith(start))
        self.assertEqual(mkv.uncompressed(b"\x00\x01", {"algo": 3, "settings": b"\x80"}, 1), b"\x80\x00\x01")
        self.assertIsNone(mkv.uncompressed(b"not zlib", self.ZLIB, mkv.TEXT_INFLATE))

    def test_image_lines_past_their_limit_are_left_out(self):
        r = Memory(fixture("tiny-pgs-zlib.mkv"))
        index = MkvIndex(r).read_head().read_cues()
        t = index.image_tracks()[0]
        starts = [s for s, _ in sorted(t["cues"])][0::2][:6]
        self.assertEqual(len(index.line_images(t["number"], starts)), 6)
        with mock.patch.object(mkv, "IMAGE_INFLATE", 50):           # each display set inflates past 50 bytes
            self.assertEqual(index.line_images(t["number"], starts), {})


class AssBlockTest(unittest.TestCase):
    def test_ass_block_text_is_its_last_field(self):
        self.assertEqual(mkv.ass_text("12,0,Default,,0,0,0,,Hello, there"), "Hello, there")


class HungLineReadTest(unittest.TestCase):
    """Once a track's line reads have stopped (three failed in a row), read_lines returns with what it
    has: a read the host never answers ends by itself at its timeout and holds nothing up."""

    def test_a_hung_read_does_not_hold_up_the_lines_once_the_reads_stopped(self):
        r = Memory(fixture("tiny.mkv"))
        index = MkvIndex(r).read_head().read_cues()
        t = index.text_tracks()[0]
        starts = [s for s, _ in t["cues"]]
        release, before = threading.Event(), len(r.reads)
        self.addCleanup(release.set)
        plain = r.get

        def get(start, length):
            n = len(r.reads)
            r.reads.append((start, length))
            if n == before:              # the first line read: a host that accepted the connection and sends nothing
                release.wait(5)
            if n >= before:
                raise ReadFailed("URLError", connection=True)
            return plain(start, length)
        r.get = get
        t0 = time.monotonic()
        self.assertEqual(index.line_words(t["number"], starts), {})
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(release.is_set())
        self.assertLessEqual(len(r.reads) - before, mkv.WORD_READS_AT_ONCE)

    def test_a_refusal_still_ends_the_reads_and_is_raised(self):
        from subsync.reader import Refused
        reads = mkv.LineReads(None)

        def one(item):
            if item == 2:
                reads.error = Refused("HTTP 403")
                reads.stop.set()
                raise reads.error
            return item, f"line {item}"
        with self.assertRaises(Refused):
            mkv.read_lines(range(4), one, reads)


class MidWindowFaultTest(unittest.TestCase):
    """A reset or a timeout in the middle of an audio window's read is a failed read (ReadFailed, a
    connection fault), as at its start: never a window decoded short and taken as whole."""

    def reader(self, data):
        return RangeReader("http://127.0.0.1:9/file.mkv", "test", opener=Opener(data))

    def test_a_streamed_read_that_breaks_off_is_a_connection_fault(self):
        r = self.reader(bytes(1_000_000))
        r.open.reset_after = 2000
        with self.assertLogs("subsync.read", "INFO") as logs, self.assertRaises(ReadFailed) as caught:
            b"".join(r.stream(0, 1_000_000, chunk=1000))
        self.assertTrue(caught.exception.connection)
        self.assertIn("got=2000", logs.output[-1])
        self.assertIn("error=ConnectionResetError", logs.output[-1])

    def test_the_decoder_feed_raises_the_reads_own_error(self):
        def chunks():
            yield b"x" * 1000
            raise ConnectionResetError(54, "Connection reset by peer")
        cat = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
        with self.assertRaises(ConnectionResetError):
            mkv.pipe_until(cat, b"", chunks(), need=100_000)

    @unittest.skipUnless(have("ffmpeg"), "ffmpeg not installed")
    def test_a_window_whose_read_breaks_off_raises(self):
        r = self.reader(fixture("tiny.mkv"))
        index = MkvIndex(r).read_head().read_cues()
        self.assertAlmostEqual(len(index.window_pcm(30.0, 10.0)) / 2 / 16000, 10.0, delta=0.1)
        index = MkvIndex(r).read_head().read_cues()
        r.open.reset_after = 1000
        with self.assertRaises(ReadFailed) as caught:
            index.window_pcm(30.0, 10.0)
        self.assertTrue(caught.exception.connection)


class DecoderCleanupTest(unittest.TestCase):
    """The decoder is stopped and waited for when the read fails, and its output pipe is closed on
    every path."""

    def started(self):
        procs, real = [], subprocess.Popen

        def popen(*a, **kw):
            procs.append(real(*a, **kw))
            return procs[-1]
        patch = mock.patch.object(mkv.subprocess, "Popen", popen)
        patch.start()
        self.addCleanup(patch.stop)
        return procs

    def test_a_failed_read_stops_the_decoder(self):
        procs = self.started()
        slow = [sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(5)"]

        def chunks():
            yield b"x" * 1000
            raise ReadFailed("short reply")
        t0 = time.monotonic()
        with self.assertRaises(ReadFailed):
            mkv.pipe_until(slow, b"", chunks(), need=10)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertIsNotNone(procs[0].returncode)        # stopped and waited for, not left decoding
        self.assertTrue(procs[0].stdout.closed)

    def test_the_output_pipe_is_closed_once_the_window_is_decoded(self):
        procs = self.started()
        out = mkv.pipe_until([sys.executable, "-c", "import sys; sys.stdout.write('done')"], b"", iter([b"x"]),
                             need=4)
        self.assertEqual(out, b"done")
        self.assertEqual(procs[0].returncode, 0)
        self.assertTrue(procs[0].stdout.closed)
        self.assertTrue(procs[0].stdin.closed)


class CutFieldTest(unittest.TestCase):
    """A size or id cut off at a buffer's end is not read as a shorter one."""

    def test_a_cut_vint_is_not_a_value(self):
        with self.assertRaises(IndexError):
            mkv.vint(bytes([0x10, 0x01]), 0)
        self.assertEqual(mkv.vint(bytes([0x10, 0x01, 0x23, 0x45]), 0), (0x12345, 4))

    def test_tracks_whose_size_runs_past_the_head_are_read_where_the_seekhead_says(self):
        data = matroska(pad_to=HEAD_BYTES - 6, seek_tracks=True)    # Tracks' id in the head, its 8-byte size cut
        index = open_index(Memory(data))
        self.assertEqual([t["codec"] for t in index.text_tracks()], ["S_TEXT/UTF8"])


class HeadSpillTest(unittest.TestCase):
    """An element that runs past the head is completed with the bytes not held yet, not read again
    from its start."""

    def test_matroska_tracks_past_the_head(self):
        big = el("1654AE6B", el("AE", el("D7", b"\x01") + el("83", b"\x11") + el("86", b"S_TEXT/ASS")
                                + el("63A2", b"[Script Info]\n" + b";" * 300_000)))
        data = matroska(tracks=big)
        r = Memory(data)
        index = MkvIndex(r).read_head()
        tracks_end = len(data) - len(el("1F43B675", el("E7", b"\x00")))   # the Cluster after them
        self.assertEqual(r.reads, [(0, HEAD_BYTES), (HEAD_BYTES, tracks_end - HEAD_BYTES)])
        self.assertEqual(len(index.tracks[0]["private"]), len(b"[Script Info]\n") + 300_000)

    def test_an_mp4_movie_box_past_the_head(self):
        data = fixture("tiny-fast.mp4")
        moov_at, moov_size = 32, 13039                              # tiny-fast.mp4: ftyp, then moov
        free = HEAD_BYTES - 5000 - moov_at                           # moved to start 5,000 bytes before the head's end
        moved = data[:moov_at] + struct.pack(">I4s", free, b"free") + bytes(free - 8) + data[moov_at:]
        r = Memory(moved)
        index = Mp4Index(r).read_head().read_cues()
        self.assertEqual(r.reads, [(0, HEAD_BYTES), (HEAD_BYTES, moov_size - 5000)])
        self.assertEqual(len(index.tracks), len(open_index(Memory(data)).tracks))


class TailReadTest(unittest.TestCase):
    """Cues (or a moov) at the end of the file are read with the file's last 64 KiB, which the hash
    then takes as it is: one read for both where there were three. The hash is the same."""

    def hashed(self, data):
        r = Memory(data)
        index = open_index(r)
        index_reads = len(r.reads)
        self.assertEqual(index.read_hash(), oshash(len(data), data[:HASH_CHUNK], data[-HASH_CHUNK:]))
        return index, r.reads, index_reads

    def test_cues_in_the_last_64_kib(self):
        data = fixture("tiny.mkv")
        index, reads, _ = self.hashed(data)
        self.assertEqual(reads, [(0, HEAD_BYTES), (len(data) - HASH_CHUNK, HASH_CHUNK)])
        self.assertEqual(len(index.text_tracks()[0]["cues"]), len(open_index(Memory(data)).text_tracks()[0]["cues"]))

    def test_cues_that_end_in_the_last_64_kib(self):
        data = fixture("tiny.mkv")
        cues_at = open_index(Memory(data)).cues_at
        padded = data + bytes(cues_at + HASH_CHUNK - len(data) + 100)   # they start 100 bytes before the last 64 KiB
        index, reads, _ = self.hashed(padded)
        self.assertEqual(len(reads), 3)                     # the head, the Cues' header, the Cues to the end
        self.assertEqual(reads[-1][0] + reads[-1][1], len(padded))
        self.assertEqual(index.text_tracks(), open_index(Memory(data)).text_tracks())

    def test_cues_far_from_the_end_are_read_as_before(self):
        data = fixture("tiny.mkv") + bytes(300 * 1024)
        index, reads, index_reads = self.hashed(data)
        self.assertEqual(index_reads, 3)
        self.assertEqual(reads[-1], (len(data) - HASH_CHUNK, HASH_CHUNK))

    def test_a_movie_box_at_the_end(self):
        data = fixture("tiny.mp4")                                    # ftyp, free, mdat, moov at 175,630
        moov_at = 175_630
        padded = data[:moov_at] + struct.pack(">I4s", 8 + 300_000, b"free") + bytes(300_000) + data[moov_at:]
        index, reads, index_reads = self.hashed(padded)
        self.assertEqual(index_reads, 3)                    # the head, the moov's header, the moov with the last 64 KiB
        self.assertEqual(len(reads), 3)
        self.assertEqual(index.text_tracks(), open_index(Memory(data)).text_tracks())


class CuePointsTest(unittest.TestCase):
    """The cue points kept are bounded (MAX_CUE_POINTS) and compact: only subtitle tracks' cues are kept per
    track, their block places in arrays."""

    def cues(self, n, track):
        """A Cues element of n cue points of `track`, all in the one cluster at segment position 0."""
        point = el("BB", el("B3", b"\x01") + el("B7", el("F7", bytes([track])) + el("F1", b"\x00")))
        return el("1C53BB6B", point * n)

    def indexed(self, n, track):
        data = matroska(tracks=el("1654AE6B", text_track(1) + el("AE", el("D7", b"\x02") + el("83", b"\x01")
                                                                          + el("86", b"V_MPEG4/ISO/AVC"))))
        index = MkvIndex(Memory(data)).read_head()
        body = self.cues(n, track)
        index.positions[mkv.CUES] = len(data)
        index.r = Memory(data + body)
        index.size = len(data) + len(body)
        return index

    def test_past_the_cap_the_file_is_not_read(self):
        with mock.patch.object(mkv, "MAX_CUE_POINTS", 100):
            self.indexed(100, 1).read_cues()
            with self.assertRaises(NotMatroska):
                self.indexed(101, 1).read_cues()

    def test_a_video_tracks_cue_points_hold_no_memory_each(self):
        index = self.indexed(100_000, 2)
        tracemalloc.start()
        try:
            index.read_cues()
            held, _ = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(index.track_cues, {})
        self.assertLess(held, 1_000_000)                    # 100,000 tuples and floats would be over 10 MB

    def test_a_subtitle_tracks_places_are_arrays_in_time_order(self):
        index = MkvIndex(Memory(fixture("tiny.mkv"))).read_head().read_cues()
        n = index.text_tracks()[0]["number"]
        times, places = index.track_places[n]
        self.assertIsInstance(times, array)
        self.assertIsInstance(places, array)
        self.assertEqual(list(times), sorted(times))
        self.assertEqual(list(times), [s for s, _ in index.track_cues[n]])


class IndexBoundTest(unittest.TestCase):
    """A Cues element larger than the parser ever reads (MAX_CUES) was skipped, so the file looked as if it
    had no subtitle track and went to the pick and the audio route; it ends as an index over the cap does."""

    def test_cues_over_the_bound_end_as_an_index_over_the_cap(self):
        data = matroska()
        header = bytes.fromhex("1C53BB6B") + b"\x01" + (mkv.MAX_CUES + 1).to_bytes(7, "big")
        index = MkvIndex(Memory(data)).read_head()
        index.positions[mkv.CUES] = len(data)
        index.r = Memory(data + header + bytes(64))
        index.size = index.r.size
        with self.assertRaises(IndexTooLarge) as caught:
            index.read_cues()
        self.assertEqual(caught.exception.cap, mkv.MAX_CUES)
        self.assertIsInstance(caught.exception, CapReached)


def flagged(number, kind, name, **flags):
    """A TrackEntry of type `kind` (0x11 subtitles, 2 audio) named `name`, with Matroska's flags (ids in hex) set."""
    codec = b"S_TEXT/UTF8" if kind == 0x11 else b"A_AAC"
    body = el("D7", bytes([number])) + el("83", bytes([kind])) + el("86", codec) + el("536E", name.encode())
    return el("AE", body + b"".join(el(eid, b"\x01") for eid in flags.values()))


class NotDialogueTest(unittest.TestCase):
    """A track name says "no dialogue" only by whole words, and not when it also says it is the full track;
    Commentary in the main languages' words, and Matroska's commentary, audio-description and
    hearing-impaired flags."""

    def test_names(self):
        for name in ("Full Subtitles (Signs & Songs)", "English [Full + Songs/Signs]", "Dialogue + Signs",
                     "Designer's cut", "Complete (with songs)", "English", "Songsmith", ""):
            self.assertFalse(mkv.named_not_dialogue(name), name)
        for name in ("Signs & Songs", "Forced", "English (Signs)", "Karaoke", "Director's Commentary",
                     "Commentaire audio du réalisateur", "Kommentar", "Comentario del director", "Commento",
                     "Commentaar", "Komentarz reżysera", "Комментарий режиссёра", "Audio Description"):
            self.assertTrue(mkv.named_not_dialogue(name), name)

    def test_flags(self):
        tracks = el("1654AE6B", flagged(1, 0x11, "Français", commentary="55AF")
                    + flagged(2, 0x11, "English", hearing="55AB")
                    + flagged(3, 0x11, "English")
                    + flagged(4, 2, "Français", default="88") + flagged(5, 2, "Audio", described="55AC")
                    + flagged(6, 2, "Kommentar"))
        index = MkvIndex(Memory(matroska(tracks=tracks))).read_head()
        text = {t["number"]: t for t in index.text_tracks()}
        self.assertEqual([(n, t["forced"], t["hearing_impaired"]) for n, t in sorted(text.items())],
                         [(1, True, False), (2, False, True), (3, False, False)])
        from subsync import audiofit, pipeline
        self.assertTrue(pipeline.is_secondary(text[2]))            # SDH by its flag, as by its name
        audio = index.audio_tracks()
        self.assertEqual([(t["commentary"], t["descriptive"], audiofit.dialogue_track(t)) for t in audio],
                         [(False, False, True), (False, True, False), (False, False, False)])


class DurationTest(unittest.TestCase):
    """A runtime that can't be one (NaN, infinite, 0, negative, longer than a week) is unknown; the last cue's
    time stands in when it is plausible."""

    def index(self, duration, cues=()):
        index = MkvIndex(None)
        index.size, index.duration_s, index.cue_points = 10 ** 9, duration, list(cues)
        return index

    def test_an_implausible_runtime_is_unknown(self):
        for bad in (float("nan"), float("inf"), -5400.0, 0.0, 1e12):
            self.assertIsNone(self.index(bad).duration(), bad)
            self.assertIsNone(audio_facts(self.index(bad)), bad)
            self.assertEqual(self.index(bad, [(10.0, 100), (95.0, 200)]).duration(), 95.0, bad)
            mp4_index = Mp4Index(None)
            mp4_index.duration_s = bad
            self.assertIsNone(mp4_index.duration(), bad)
        self.assertEqual(self.index(5400.0).duration(), 5400.0)
        self.assertEqual(audio_facts(self.index(5400.0))["rate"], round(10 ** 9 / 5400))

    def test_a_nan_duration_in_the_file(self):
        info = el("1549A966", el("2AD7B1", (1_000_000).to_bytes(4, "big")) + el("4489", struct.pack(">d", math.nan)))
        index = open_index(Memory(matroska(info=info)))
        self.assertIsNone(index.duration())
        self.assertIsNone(audio_facts(index))


class DamagedFieldTest(unittest.TestCase):
    """A damaged or made-up field makes the file not readable (NotMatroska, which the worker
    backs off from), or is left out, never another error that has the file read again on every request."""

    def test_a_duration_of_another_size_is_left_out_and_one_of_none_is_0(self):
        for raw in (b"\x01\x02", b"\x01\x02\x03", b""):
            info = el("1549A966", el("2AD7B1", (1_000_000).to_bytes(4, "big")) + el("4489", raw))
            index = open_index(Memory(matroska(info=info)))
            self.assertEqual(len(index.tracks), 1)
            self.assertIsNone(index.duration())
        self.assertAlmostEqual(open_index(Memory(matroska())).duration(), 90.0)

    def test_matroska_damage_is_not_matroska(self):
        cases = {
            "the head ends after the EBML header": matroska()[:21],
            "a timestamp scale no float holds": matroska(info=el("1549A966", el("2AD7B1", b"\xff" * 400)
                                                                 + el("4489", struct.pack(">d", 1.0)))),
            "no bytes at all": b"",
        }
        for what, data in cases.items():
            with self.assertRaises(NotMatroska, msg=what):
                open_index(Memory(data))

    def test_mp4_damage_is_not_matroska(self):
        data = bytearray(fixture("tiny.mp4"))
        stts = data.find(b"stts")
        struct.pack_into(">I", data, stts + 8, 0xFFFFFFFF)      # more entries than the movie box holds
        with self.assertRaises(NotMatroska):
            open_index(Memory(data))

        def box(kind, payload):
            return struct.pack(">I4s", 8 + len(payload), kind) + payload
        nested = b""
        for _ in range(3000):                                        # boxes inside boxes, deeper than any file
            nested = box(b"mdia", nested)
        deep = box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", box(b"trak", nested))
        with self.assertRaises(NotMatroska):
            open_index(Memory(deep))

    def test_an_mp4_track_with_a_timescale_of_0_is_left_out(self):
        data = bytearray(fixture("tiny.mp4"))
        good = open_index(Memory(bytes(data)))
        text = next(t for t in good.tracks if t["codec"] == b"tx3g")
        at = 0
        while (at := data.find(b"mdhd", at + 1)) >= 0:              # the text track's media header
            scale_at = at + 4 + (20 if data[at + 4] == 1 else 12)
            if struct.unpack_from(">I", data, scale_at)[0] == text["scale"]:
                struct.pack_into(">I", data, scale_at, 0)
        index = open_index(Memory(bytes(data)))
        self.assertEqual(index.text_tracks(), [])
        self.assertEqual(len(index.tracks), len(good.tracks) - sum(t["scale"] == text["scale"] for t in good.tracks))


class ClusterRangeTest(unittest.TestCase):
    """A cluster's time in the Cues is its first cue's, a subtitle's when it holds no keyframe, so a cluster
    cued past the window may begin inside it: the range runs to the cue cluster after that one."""

    def index(self):
        # gop.mkv's cue points (B024-A): clusters really start at 0, 4.5, 10, 14.5, 20, 24.5, 30 s; the 4.5 s and 24.5 s
        # ones are cued by subtitle lines at 7 s and 27 s
        index = MkvIndex(None)
        index.cue_points = [(0.0, 5698), (7.0, 21966), (10.0, 37494), (20.0, 68131), (27.0, 83259), (30.0, 98768)]
        index.size, index.cues_at, index.first_cluster = 120_000, 110_000, 5698
        return index

    def test_the_cluster_cued_late_is_read_whole(self):
        index = self.index()
        self.assertEqual(index.byte_range(25.5, 1.0), (68131, 98768))   # the cluster from 24.5 s holds 25.5-26.5
        self.assertEqual(index.byte_range(3.0, 3.0), (5698, 37494))     # the cluster from 4.5 s holds 4.5-6
        self.assertEqual(index.byte_range(28.0, 1.0), (83259, 110_000))  # no cue cluster after: up to the Cues
        self.assertEqual(index.byte_range(31.0, 1.0), (98768, 110_000))


if __name__ == "__main__":
    unittest.main()
