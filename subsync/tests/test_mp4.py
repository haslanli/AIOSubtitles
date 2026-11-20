"""The MP4 index on damaged or made-up movie boxes, and its audio windows: sample tables read only as far as their box
goes, sample counts a few bytes can claim kept within bounds, a movie length the header leaves unknown, the esds fields
in their order, and windows streamed into the decoder, never further than their share of the file. No network: files
are served from memory."""
import struct
import sys
import time
import unittest
from unittest import mock

from helpers import fixture, have, Memory, Opener  # noqa: E402
from subsync import mp4  # noqa: E402
from subsync.mp4 import NotMp4, open_index  # noqa: E402
from subsync.reader import IndexTooLarge, RangeReader  # noqa: E402

CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts")


def tracks(data):
    """Each track's boxes, in file order: {type: (payload start, end)}, the first of each type."""
    found = []

    def walk(p, e, into):
        for kind, q, f in mp4.boxes(data, p, e):
            if kind == b"trak":
                found.append({})
                walk(q, f, found[-1])
            elif kind in CONTAINERS:
                walk(q, f, into)
            elif into is not None:
                into.setdefault(kind, (q, f))
    walk(0, len(data), None)
    return found


def track(data, codec):
    """The boxes of the track whose sample entry is `codec`."""
    return next(t for t in tracks(data) if data[t[b"stsd"][0] + 12:t[b"stsd"][0] + 16] == codec)


def top(data, kind):
    """(start, end) of a top-level box."""
    return next((q - 8, f) for k, q, f in mp4.boxes(data, 0, len(data)) if k == kind)


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def rebuilt(data, edit):
    """tiny.mp4 (its movie box at the end, so no sample moves) with the boxes of each track rebuilt by
    `edit(kind, payload, parent) -> payload`, applied inside out."""
    def walk(p, e, parent):
        out = b""
        for kind, q, f in mp4.boxes(data, p, e):
            payload = walk(q, f, kind) if kind in (b"moov", b"trak", b"mdia", b"minf") else data[q:f]
            out += box(kind, edit(kind, payload, parent))
        return out
    return walk(0, len(data), None)


def quicktime(kind, payload, parent):
    """A box as QuickTime's (and ffmpeg's MOV muxer's) writer has it: the media handler with its component type and a
    counted name, and a data handler ("dhlr", "url ") first in each track's minf."""
    if kind == b"hdlr" and parent == b"mdia":
        name = payload[24:].split(b"\0")[0]
        return payload[:4] + b"mhlr" + payload[8:24] + bytes([len(name)]) + name
    if kind == b"minf":
        return box(b"hdlr", b"\0\0\0\0dhlr" + b"url " + bytes(12) + b"\x0bDataHandler") + payload
    return payload


class QuickTimeTest(unittest.TestCase):
    """A .mov's data handler in minf overwrote each track's kind, so the file seemed to have no sound and no
    text track; and a QuickTime handler's name is a counted string."""

    def test_a_movs_tracks_keep_their_kind_and_name(self):
        data = fixture("tiny.mp4")
        index = open_index(Memory(data))
        mov = open_index(Memory(rebuilt(data, quicktime)))
        self.assertEqual([t["handler"] for t in mov.tracks], [t["handler"] for t in index.tracks])
        self.assertEqual([t["name"] for t in mov.tracks], [t["name"] for t in index.tracks])
        self.assertIsNotNone(mov._audio())
        self.assertEqual([(t["number"], len(t["cues"])) for t in mov.text_tracks()],
                         [(t["number"], len(t["cues"])) for t in index.text_tracks()])
        self.assertTrue(mov.text_tracks())


class FragmentedTest(unittest.TestCase):
    """A fragmented MP4 (an mvex box in its movie box) opened as an MP4 with no timings, so it waited for a pick
    and spent an audio sync on a file that always keeps its originals; it is no MP4 this module reads, as an AVI."""

    def test_a_fragmented_mp4_is_not_read(self):
        mvex = box(b"mvex", box(b"trex", bytes(24)))
        data = rebuilt(fixture("tiny.mp4"), lambda kind, payload, parent: payload + mvex if kind == b"moov" else payload)
        with self.assertRaisesRegex(NotMp4, "fragmented MP4"):
            open_index(Memory(data))


class MovieBoxBoundTest(unittest.TestCase):
    """A movie box larger than MAX_MOOV was "not readable", tried again after 1, 6 and 24 hours; it ends as an
    index over the cap does."""

    def test_a_movie_box_over_the_bound_ends_as_an_index_over_the_cap(self):
        with mock.patch.object(mp4, "MAX_MOOV", 1000), self.assertRaises(IndexTooLarge) as caught:
            open_index(Memory(fixture("tiny.mp4")))
        self.assertEqual(caught.exception.cap, 1000)


class TableTest(unittest.TestCase):
    """A sample table is read only within its own box; a count that claims more entries than the box holds
    makes the file unreadable (NotMp4), never entries taken from the boxes after it."""

    def test_a_table_claiming_more_entries_than_its_box_holds_is_not_read(self):
        data = fixture("tiny-fast.mp4")
        text = track(data, b"tx3g")
        for kind, count_at in ((b"stts", 4), (b"stsz", 8), (b"stsc", 4), (b"stco", 4), (b"elst", 4)):
            bad = bytearray(data)
            q, _ = text[kind]
            n = struct.unpack_from(">I", bad, q + count_at)[0]
            struct.pack_into(">I", bad, q + count_at, n + 3)     # three entries more: the next box's bytes
            with self.subTest(kind=kind), self.assertRaises(NotMp4):
                open_index(Memory(bytes(bad)))

    def test_tables_that_fit_their_boxes_are_read_as_before(self):
        index = open_index(Memory(fixture("tiny-fast.mp4")))
        text = next(t for t in index.tracks if t["codec"] == b"tx3g")
        self.assertEqual((len(text["stts"]), len(text["sizes"]), text["count"]), (55, 61, 61))


class SampleCountTest(unittest.TestCase):
    """A constant sample size lets a few bytes of the movie box claim billions of samples, each a Python object
    or more once its track is walked: past MAX_TEXT_SAMPLES (all text tracks) or MAX_AUDIO_SAMPLES (the audio track
    windows are cut from) the file is not read. Tracks never walked one sample at a time may have any number."""

    def claiming(self, codec, n, data=None):
        """tiny-fast.mp4 with the `codec` track's samples all of one size, n of them, in one run of one tick each."""
        data = bytearray(fixture("tiny-fast.mp4") if data is None else data)
        boxes = track(bytes(data), codec)
        struct.pack_into(">II", data, boxes[b"stsz"][0] + 4, 100, n)
        struct.pack_into(">III", data, boxes[b"stts"][0] + 4, 1, n, 1)
        return bytes(data)

    def test_a_track_claiming_billions_of_samples_is_not_read(self):
        for codec in (b"tx3g", b"mp4a"):
            with self.subTest(codec=codec):
                t0 = time.monotonic()
                with self.assertRaises(NotMp4):
                    open_index(Memory(self.claiming(codec, 0xFFFFFFFF)))
                self.assertLess(time.monotonic() - t0, 1.0)

    def test_the_limits(self):
        for codec, limit in ((b"tx3g", "MAX_TEXT_SAMPLES"), (b"mp4a", "MAX_AUDIO_SAMPLES")):
            with self.subTest(codec=codec), mock.patch.object(mp4, limit, 2000):
                index = open_index(Memory(self.claiming(codec, 2000)))         # at the limit: read
                if codec == b"tx3g":
                    self.assertEqual(len(index.text_tracks()[0]["cues"]), 2000)
                with self.assertRaises(NotMp4):
                    open_index(Memory(self.claiming(codec, 2001)))

    def test_a_track_never_walked_may_claim_any_number(self):
        # a QuickTime PCM track counts one sample per audio sample: 3 hours at 48 kHz are 518,400,000
        data = bytearray(self.claiming(b"mp4a", 518_400_000))
        q, _ = track(fixture("tiny-fast.mp4"), b"mp4a")[b"stsd"]
        data[q + 12:q + 16] = b"sowt"
        index = open_index(Memory(bytes(data)))
        self.assertEqual(index.text_tracks(), open_index(Memory(fixture("tiny-fast.mp4"))).text_tracks())


class MovieLengthTest(unittest.TestCase):
    """A movie header's length of all ones means "unknown" (ISO 14496-12), never 49.7 days or 4,295 s; the
    audio track's own end stands in, as it does for no length at all."""

    def with_length(self, version, value):
        """tiny.mp4 (its movie box ends the file) with the movie header's length set, as a version 0 or 1 header."""
        data = fixture("tiny.mp4")
        moov_at, moov_end = top(data, b"moov")
        mvhd_at = moov_at + 8
        size = struct.unpack_from(">I", data, mvhd_at)[0]
        p = mvhd_at + 8                                            # its payload: version and flags, then the times
        scale = struct.unpack_from(">I", data, p + 12)[0]
        rest = data[p + 20:mvhd_at + size]                         # rate, volume, matrix, next track id
        if version == 0:
            body = bytes(4) + bytes(8) + struct.pack(">II", scale, value) + rest
        else:
            body = b"\x01\x00\x00\x00" + bytes(16) + struct.pack(">IQ", scale, value) + rest
        mvhd = struct.pack(">I4s", 8 + len(body), b"mvhd") + body
        moov = data[mvhd_at + size:moov_end]
        return data[:moov_at] + struct.pack(">I4s", 8 + len(mvhd) + len(moov), b"moov") + mvhd + moov

    def test_an_unknown_or_missing_movie_length_is_the_audio_tracks_end(self):
        self.assertAlmostEqual(open_index(Memory(self.with_length(0, 94_000_000))).duration(), 94.0)   # as before
        for version, value in ((0, 0xFFFFFFFF), (1, 0xFFFFFFFFFFFFFFFF), (0, 0)):
            with self.subTest(version=version, value=value):
                index = open_index(Memory(self.with_length(version, value)))
                self.assertAlmostEqual(index.duration(), 90.048, places=3)   # 1408 AAC frames at 16 kHz, less the delay


class EsdsTest(unittest.TestCase):
    """An ES descriptor's optional fields come in the order ISO 14496-1 gives them (dependsOn_ES_ID, URL,
    OCR_ES_Id), so the decoder config after them is found whichever are present."""

    def entry(self, flags):
        es = b"\x00\x01" + bytes([flags])                         # ES_ID, flags
        if flags & 0x80:
            es += b"\x00\x02"                                     # dependsOn_ES_ID
        if flags & 0x40:
            es += b"\x04http"                                     # URL length, URL
        if flags & 0x20:
            es += b"\x00\x03"                                     # OCR_ES_Id
        config = bytes([0x40, 0x15]) + bytes(11) + b"\x05\x02\x12\x10"   # AAC; AAC LC, 44.1 kHz, stereo
        es += bytes([0x04, len(config)]) + config
        return bytes(28) + b"esds" + bytes(4) + bytes([0x03, len(es)]) + es

    def test_every_combination_of_the_optional_fields(self):
        for flags in (0x00, 0x80, 0x40, 0x20, 0xC0, 0xA0, 0x60, 0xE0):
            with self.subTest(flags=hex(flags)):
                self.assertEqual(mp4._audio_specific_config(self.entry(flags)), b"\x02\x04\x02")


class SampleTextTest(unittest.TestCase):
    """A text sample's text ((5): WebVTT samples had no test): tx3g a 16-bit length then UTF-8; wvtt the
    payloads of its cue boxes, an empty cue (vtte) giving none."""

    def test_webvtt_cues_give_their_payloads(self):
        sample = (box(b"vttc", box(b"sttg", b"line:90%") + box(b"payl", b"Hello there.")) + box(b"vtte", b"")
                  + box(b"vttc", box(b"iden", b"2") + box(b"payl", "Général Kenobi.".encode())))
        self.assertEqual(mp4.sample_text(b"wvtt", sample), "Hello there.\nGénéral Kenobi.")
        self.assertIsNone(mp4.sample_text(b"wvtt", box(b"vtte", b"")))
        self.assertIsNone(mp4.sample_text(b"wvtt", b""))

    def test_tx3g_reads_its_length_and_nothing_past_it(self):
        self.assertEqual(mp4.sample_text(b"tx3g", struct.pack(">H", 2) + b"Hi" + b"styl..."), "Hi")
        self.assertIsNone(mp4.sample_text(b"tx3g", b"\x00"))
        self.assertIsNone(mp4.sample_text(b"stpp", b"<tt/>"))


class WindowTest(unittest.TestCase):
    """An audio window's samples are streamed into the decoder as their bytes pass, never read whole
    into memory with the video between them; a window whose samples spread far past its share of the file is not
    read."""

    def reader(self, data):
        return RangeReader("http://127.0.0.1:9/file.mp4", "test", opener=Opener(data))

    @unittest.skipUnless(have("ffmpeg"), "ffmpeg not installed")
    def test_a_window_is_streamed_and_cut_as_its_bytes_pass(self):
        data = fixture("tiny.mp4")
        whole = open_index(self.reader(data)).window_pcm(20.0, 10.0)     # its range in one piece
        self.assertAlmostEqual(len(whole) / 2 / 16000, 10.0, delta=0.1)
        r = self.reader(data)
        index = open_index(r)

        def buffered(*a):
            raise AssertionError("an audio window read whole into memory")
        r.get = buffered
        before = r.bytes
        pcm = index.window_pcm(20.0, 10.0, chunk=1000)                   # samples cut across 1,000-byte pieces
        self.assertEqual(pcm, whole)
        self.assertEqual(index.audio_bytes, r.bytes - before)

    def test_samples_stored_out_of_order_are_fed_in_decoding_order(self):
        data = bytearray(30)
        for at, frame in ((10, b"AAA"), (4, b"BBB"), (16, b"CCC"), (22, b"DDD")):
            data[at:at + 3] = frame
        index = mp4.Mp4Index(self.reader(bytes(data)))
        index.size, index.duration_s = len(data), 4.0
        index.tracks = [{"id": 1, "handler": b"soun", "codec": b"ac-3", "enabled": True, "asc": b"", "scale": 1,
                         "stts": [(4, 1)], "const": 3, "sizes": [], "count": 4, "stsc": [(1, 1, 1)],
                         "chunks": [10, 4, 16, 22], "empty": 0, "media_time": 0}]   # one 3-byte sample per chunk
        echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
        with mock.patch.object(mp4, "decoder_cmd", lambda *a, **k: echo):
            self.assertEqual(index.window_pcm(0.0, 4.0, chunk=2), b"AAABBBCCCDDD")   # cut across 2-byte pieces
        self.assertEqual(index.audio_bytes, 25 - 4)                                    # from the first stored on

    def test_a_windows_cost_is_the_byte_range_it_reads(self):
        # The audio budget counted an MP4 window by the file's average byte rate, while its read is the byte
        # range of its samples (up to MAX_WINDOW_SPAN times its share)
        data = bytearray(30)
        for at, frame in ((10, b"AAA"), (4, b"BBB"), (16, b"CCC"), (22, b"DDD")):
            data[at:at + 3] = frame
        index = mp4.Mp4Index(self.reader(bytes(data)))
        index.size, index.duration_s = len(data), 4.0
        index.tracks = [{"id": 1, "handler": b"soun", "codec": b"ac-3", "enabled": True, "asc": b"", "scale": 1,
                         "stts": [(4, 1)], "const": 3, "sizes": [], "count": 4, "stsc": [(1, 1, 1)],
                         "chunks": [10, 4, 16, 22], "empty": 0, "media_time": 0}]
        self.assertEqual(index.window_bytes(0.0, 4.0), 25 - 4)          # from the first stored to the last's end
        self.assertEqual(index.window_bytes(1.5, 1.0), 19 - 4)          # samples 1 to 2 (and the margin's)
        self.assertEqual(index.window_bytes(10.0, 4.0), 0)              # no sample there: read as no audio
        echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
        with mock.patch.object(mp4, "decoder_cmd", lambda *a, **k: echo):
            index.window_pcm(0.0, 4.0)
        self.assertEqual(index.audio_bytes, index.window_bytes(0.0, 4.0))
        index.tracks = []
        with self.assertRaises(NotMp4):                                 # no audio to decode, as window_pcm says
            index.window_bytes(0.0, 4.0)

    def test_a_window_whose_samples_spread_far_past_it_is_not_read(self):
        data = bytearray(fixture("tiny.mp4"))
        q, _ = track(bytes(data), b"mp4a")[b"stco"]
        n = struct.unpack_from(">I", data, q + 4)[0]
        for k in range(1, n):                                    # every chunk but the first: near the file's end
            struct.pack_into(">I", data, q + 8 + 4 * k, len(data) - 5000)
        r = self.reader(bytes(data))
        index = open_index(r)
        before = r.bytes
        with self.assertRaises(NotMp4):                          # (AudioFit then keeps the original, with the reason)
            index.window_pcm(0.0, 5.0)
        self.assertEqual(r.bytes, before)                        # nothing read


if __name__ == "__main__":
    unittest.main()
