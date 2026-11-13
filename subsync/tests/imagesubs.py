"""Image subtitles written by the tests: PGS display sets and VobSub subpictures of simple bitmaps, so the
decoders can be checked without any real file. A "line" is a filled bar with a 1-pixel dark outline on a transparent
ground, its width standing for its text (the tests' fake recogniser reads the width)."""
import struct

import numpy as np

# palette indexes: 0 transparent, 1 white fill, 2 black outline
PGS_PALETTE = [(0, 16, 128, 128, 0), (1, 235, 128, 128, 255), (2, 16, 128, 128, 255)]


def bar(width, height=24):
    """A text row stand-in: index 1 inside, 2 on its 1-pixel border."""
    img = np.full((height, width), 2, dtype=np.uint8)
    img[1:-1, 1:-1] = 1
    return img


def stack(*rows, gap=8):
    """Rows one above the other, `gap` transparent pixel rows between, as wide as the widest."""
    w = max(r.shape[1] for r in rows)
    parts = []
    for i, r in enumerate(rows):
        padded = np.zeros((r.shape[0], w), dtype=np.uint8)
        padded[:, :r.shape[1]] = r
        parts.append(padded)
        if i + 1 < len(rows):
            parts.append(np.zeros((gap, w), dtype=np.uint8))
    return np.vstack(parts)


# -- PGS ---------------------------------------------------------------------------------------------------------------
def pgs_rle(img):
    out = bytearray()
    for row in img:
        x = 0
        while x < len(row):
            c = int(row[x])
            n = 1
            while x + n < len(row) and row[x + n] == c and n < 16383:
                n += 1
            if c != 0 and n < 3:
                out += bytes([c]) * n
            elif c == 0:
                out += bytes([0, n]) if n < 64 else bytes([0, 0x40 | (n >> 8), n & 0xFF])
            else:
                out += bytes([0, 0x80 | n, c]) if n < 64 else bytes([0, 0xC0 | (n >> 8), n & 0xFF, c])
            x += n
        out += b"\x00\x00"
    return bytes(out)


def _seg(kind, data):
    return bytes([kind]) + struct.pack(">H", len(data)) + data


def pgs_show(img, x=100, y=900, number=1, split=0):
    """A display set (segments as Matroska stores them: no "PG" header) that shows `img`. split > 0 cuts the object's
    run-length data into several object segments of at most `split` bytes."""
    h, w = img.shape
    pcs = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, number, 0x80, 0, 0, 1) + struct.pack(">HBBHH", 0, 0, 0, x, y)
    wds = bytes([1, 0]) + struct.pack(">HHHH", x, y, w, h)
    pds = bytes([0, 0]) + b"".join(bytes(e) for e in PGS_PALETTE)
    rle = pgs_rle(img)
    first = struct.pack(">HBB", 0, 0, 0x80 | (0x40 if not split else 0)) + (len(rle) + 4).to_bytes(3, "big") + \
        struct.pack(">HH", w, h)
    if not split:
        ods = [_seg(0x15, first + rle)]
    else:
        chunks = [rle[i:i + split] for i in range(0, len(rle), split)]
        ods = []
        for k, c in enumerate(chunks):
            if k == 0:
                head = bytearray(first)
                head[3] = 0x80 | (0x40 if len(chunks) == 1 else 0)
                ods.append(_seg(0x15, bytes(head) + c))
            else:
                flag = 0x40 if k == len(chunks) - 1 else 0x00
                ods.append(_seg(0x15, struct.pack(">HBB", 0, 0, flag) + c))
    return _seg(0x16, pcs) + _seg(0x17, wds) + _seg(0x14, pds) + b"".join(ods) + _seg(0x80, b"")


def pgs_clear(number=2, x=100, y=900, w=10, h=10):
    pcs = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, number, 0x00, 0, 0, 0)
    return _seg(0x16, pcs) + _seg(0x17, bytes([1, 0]) + struct.pack(">HHHH", x, y, w, h)) + _seg(0x80, b"")


def sup(events):
    """A .sup file: [(seconds, display set)] with each segment given its "PG" header and 90 kHz times."""
    out = bytearray()
    for t, ds in events:
        pts = int(round(t * 90000))
        p = 0
        while p < len(ds):
            size = struct.unpack_from(">H", ds, p + 1)[0]
            out += b"PG" + struct.pack(">II", pts, 0) + ds[p:p + 3 + size]
            p += 3 + size
    return bytes(out)


# -- VobSub -------------------------------------------------------------------------------------------------------------
VOBSUB_PALETTE = ["000000", "f0f0f0", "101010"] + ["808080"] * 13   # the .idx palette (RGB)


class _Nibbles:
    def __init__(self):
        self.n = []

    def code(self, run, color):
        v = (run << 2) | color
        if run < 4:
            self.n += [v]
        elif run < 16:
            self.n += [v >> 4, v & 0xF]
        elif run < 64:
            self.n += [v >> 8, (v >> 4) & 0xF, v & 0xF]
        else:
            self.n += [v >> 12, (v >> 8) & 0xF, (v >> 4) & 0xF, v & 0xF]

    def end_line(self):
        if len(self.n) % 2:
            self.n.append(0)

    def bytes(self):
        n = self.n + ([0] if len(self.n) % 2 else [])
        return bytes((a << 4) | b for a, b in zip(n[::2], n[1::2]))


def _field(rows):
    nb = _Nibbles()
    for row in rows:
        x = 0
        while x < len(row):
            c = int(row[x])
            n = 1
            while x + n < len(row) and row[x + n] == c and n < 255:
                n += 1
            nb.code(n, c)
            x += n
        nb.end_line()
    return nb.bytes()


def vobsub_spu(img, x=200, y=400):
    """One subpicture: the two fields' run-length data, then one control sequence (start display, palette, contrast,
    area, field offsets)."""
    h, w = img.shape
    top, bottom = _field(img[0::2]), _field(img[1::2])
    data_start = 4
    top_at, bottom_at = data_start, data_start + len(top)
    ctrl_at = bottom_at + len(bottom)
    x2, y2 = x + w - 1, y + h - 1
    area = bytes([x >> 4, ((x & 0xF) << 4) | (x2 >> 8), x2 & 0xFF, y >> 4, ((y & 0xF) << 4) | (y2 >> 8), y2 & 0xFF])
    # palette and contrast give pixel values 3, 2, 1, 0 in that nibble order: 2 the dark outline (palette 2), 1 the
    # light fill (palette 1), 0 transparent
    cmds = bytes([0x01]) + bytes([0x03, 0x02, 0x10]) + bytes([0x04, 0x0F, 0xF0]) + bytes([0x05]) + area + \
        bytes([0x06]) + struct.pack(">HH", top_at, bottom_at) + bytes([0xFF])
    ctrl = struct.pack(">HH", 0, ctrl_at) + cmds
    body = top + bottom + ctrl
    return struct.pack(">HH", 4 + len(body), ctrl_at) + body


def vobsub_private(width=720, height=480):
    """The track's CodecPrivate: the .idx header with its size and 16-colour palette."""
    return (f"size: {width}x{height}\npalette: " + ", ".join(VOBSUB_PALETTE) + "\n").encode()
