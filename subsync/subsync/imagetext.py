"""Words read off image subtitle lines: PGS and VobSub decoded here, the text rows cut apart and read by a line
recogniser, so `textpair` can pair a subtitle with an image track in its language as with a text track.

PGS (S_HDMV/PGS): a Matroska block is one display set, its segments without the "PG" header: palette (0x14: entry, Y,
Cr, Cb, alpha), object (0x15: run-length coded, possibly split over several segments), composition (0x16), window
(0x17), end (0x80). VobSub (S_VOBSUB): a block is one subpicture: two interlaced fields of 2-bit run-length data and a
control sequence naming four of the 16 colours of the track's palette (its CodecPrivate, the .idx text) and their
contrast. Both become a grey image of dark text on white (the fill is the bright part of a subtitle): each pixel 255 -
luminance x alpha. A subtitle has one to three text rows, cut apart where no row holds text (rows); each row is scaled to
the recogniser's input height (to_input).

The recogniser (E2): PP-OCRv5 mobile line recognition (PaddleOCR, Apache-2.0) on onnxruntime, the Latin model
for Latin-script languages and the Eastern Slavic one for Russian, Ukrainian, Belarusian and Bulgarian; its output is
decoded greedily (CTC: repeats and blanks dropped). The models are files in MODEL_DIR ({name}_rec.onnx and
{name}_dict.txt); without them there is no recogniser, and image tracks stay timing-only as before. Reading stops after
BUDGET_S seconds per track. Nothing here reads a file: index.line_images does, within D1's limits.
"""
import os
import struct
import threading
import time

import numpy as np

from .langs import lang3

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr")
HEIGHT = 48           # the recogniser's input height
MAX_WIDTH = 1600
INK = 128             # a pixel darker than this is text
ROW_GAP = 2           # empty pixel rows that separate two text rows
ROW_PAD = 3
MIN_ROW = 6
BUDGET_S = 20.0
LATIN = {"eng", "fre", "ger", "spa", "ita", "por", "dut", "swe", "nor", "nob", "nno", "dan", "fin", "pol", "cze", "slo",
         "hun", "rum", "hrv", "slv", "bos", "est", "lav", "lit", "tur", "ind", "may", "cat", "glg", "baq", "ice", "gle",
         "alb", "afr", "swa", "tgl", "lat", "wel"}
ESLAV = {"rus", "ukr", "bel", "bul"}


def model_for(lang):
    """The recogniser model for a language: "latin", "eslav", or None (no words for it)."""
    code = lang3(lang)
    return "latin" if code in LATIN else "eslav" if code in ESLAV else None


# -- PGS -----------------------------------------------------------------------------------------------------------------
def _segments(buf):
    p = 0
    while p + 3 <= len(buf):
        kind, size = buf[p], struct.unpack_from(">H", buf, p + 1)[0]
        if p + 3 + size > len(buf):
            return
        yield kind, buf[p + 3:p + 3 + size]
        p += 3 + size


def _pgs_rle(data, w, h):
    out = np.zeros((h, w), dtype=np.uint8)
    x = y = i = 0
    n = len(data)
    while i < n and y < h:
        b = data[i]
        i += 1
        if b:
            if x < w:
                out[y, x] = b
            x += 1
            continue
        if i >= n:
            break
        f = data[i]
        i += 1
        if f == 0:
            y, x = y + 1, 0
            continue
        run = f & 0x3F
        if f & 0x40:
            if i >= n:
                break
            run = (run << 8) | data[i]
            i += 1
        c = 0
        if f & 0x80:
            if i >= n:
                break
            c = data[i]
            i += 1
        out[y, x:min(w, x + run)] = c
        x += run
    return out


def pgs_images(payload):
    """The images a PGS display set shows: [grey uint8 array, dark text on white], in the order of their objects; []
    for a clear, an update without objects, or damaged data."""
    try:
        palette, objects, order = {}, {}, []
        for kind, d in _segments(payload):
            if kind == 0x14:
                for k in range(2, len(d) - 4, 5):
                    palette[d[k]] = (d[k + 1], d[k + 4])
            elif kind == 0x15 and len(d) >= 4:
                oid, flag = struct.unpack_from(">H", d, 0)[0], d[3]
                if flag & 0x80 and len(d) >= 11:
                    w, h = struct.unpack_from(">HH", d, 7)
                    if oid not in objects:
                        order.append(oid)
                    objects[oid] = {"w": w, "h": h, "data": bytearray(d[11:])}
                elif oid in objects:
                    objects[oid]["data"] += d[4:]
        lum = np.full(256, 255, dtype=np.uint8)
        for k, (y, a) in palette.items():
            lum[k] = 255 - int(round(y * a / 255))
        out = []
        for oid in order:
            o = objects[oid]
            if not o["w"] or not o["h"] or o["w"] * o["h"] > 4096 * 1024:
                continue
            out.append(lum[_pgs_rle(bytes(o["data"]), o["w"], o["h"])])
        return out
    except (struct.error, IndexError, ValueError):
        return []


# -- VobSub --------------------------------------------------------------------------------------------------------------
def vobsub_palette(private):
    """The 16 colours' luminance (0-255) from a VobSub track's CodecPrivate (the .idx header), or None."""
    for line in (private or b"").decode("latin-1").splitlines():
        if line.lower().startswith("palette:"):
            cols = [c.strip() for c in line.split(":", 1)[1].split(",")]
            try:
                rgb = [int(c, 16) for c in cols[:16]]
            except ValueError:
                return None
            if len(rgb) < 16:
                return None
            return [int(round(0.299 * (v >> 16) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255))) for v in rgb]
    return None


def _vob_field(data, start, w, rows):
    out = np.zeros((rows, w), dtype=np.uint8)
    pos = start * 2

    def nib():
        nonlocal pos
        b = data[pos >> 1]
        v = b >> 4 if pos % 2 == 0 else b & 0xF
        pos += 1
        return v
    for r in range(rows):
        x = 0
        while x < w:
            v = nib()
            if v < 0x4:
                v = (v << 4) | nib()
                if v < 0x10:
                    v = (v << 4) | nib()
                    if v < 0x40:
                        v = (v << 4) | nib()
            run, c = v >> 2, v & 3
            if run == 0:
                run = w - x
            out[r, x:min(w, x + run)] = c
            x += run
        if pos % 2:
            pos += 1
    return out


def vobsub_image(spu, private):
    """The grey image (dark text on white) of one VobSub subpicture, or None."""
    lums = vobsub_palette(private)
    if lums is None:
        return None
    try:
        at = struct.unpack_from(">H", spu, 2)[0]
        colors = alpha = area = fields = None
        seen = set()
        while at not in seen and at + 4 <= len(spu):
            seen.add(at)
            nxt = struct.unpack_from(">H", spu, at + 2)[0]
            p = at + 4
            while p < len(spu):
                cmd = spu[p]
                p += 1
                if cmd == 0xFF:
                    break
                if cmd in (0x00, 0x01, 0x02):
                    continue
                if cmd == 0x03:
                    colors = [spu[p + 1] & 0xF, spu[p + 1] >> 4, spu[p] & 0xF, spu[p] >> 4]   # values 0, 1, 2, 3
                    p += 2
                elif cmd == 0x04:
                    alpha = [spu[p + 1] & 0xF, spu[p + 1] >> 4, spu[p] & 0xF, spu[p] >> 4]
                    p += 2
                elif cmd == 0x05:
                    b = spu[p:p + 6]
                    area = ((b[0] << 4) | (b[1] >> 4), ((b[1] & 0xF) << 8) | b[2],
                            (b[3] << 4) | (b[4] >> 4), ((b[4] & 0xF) << 8) | b[5])
                    p += 6
                elif cmd == 0x06:
                    fields = struct.unpack_from(">HH", spu, p)
                    p += 4
                else:
                    break
            if nxt == at:
                break
            at = nxt
        if not (colors and alpha and area and fields):
            return None
        x1, x2, y1, y2 = area
        w, h = x2 - x1 + 1, y2 - y1 + 1
        if w <= 0 or h <= 0 or w * h > 2048 * 1024:
            return None
        img = np.zeros((h, w), dtype=np.uint8)
        img[0::2] = _vob_field(spu, fields[0], w, (h + 1) // 2)
        img[1::2] = _vob_field(spu, fields[1], w, h // 2)
        lut = np.array([255 - int(round(lums[colors[v]] * alpha[v] / 15)) for v in range(4)], dtype=np.uint8)
        return lut[img]
    except (struct.error, IndexError, ValueError):
        return None


# -- rows and the recogniser's input -------------------------------------------------------------------------------------
def rows(gray):
    """The text rows of an image, top to bottom, each the image's full width with ROW_PAD pixels above and below."""
    has = (gray < INK).any(axis=1)
    out, a, gap = [], None, 0
    for y, on in enumerate(list(has) + [False] * (ROW_GAP + 1)):
        if on:
            if a is None:
                a = y
            gap, last = 0, y
        elif a is not None:
            gap += 1
            if gap > ROW_GAP:
                if last + 1 - a >= MIN_ROW:
                    out.append(gray[max(0, a - ROW_PAD):min(len(gray), last + 1 + ROW_PAD)])
                a, gap = None, 0
    return out


def to_input(gray, height=HEIGHT):
    """A row scaled (bilinear) to `height`, as the recogniser's input: float32 (1, 3, height, width) in [-1, 1]."""
    h, w = gray.shape
    nw = max(8, min(MAX_WIDTH, int(round(w * height / h))))
    ys = np.linspace(0, h - 1, height)
    xs = np.linspace(0, w - 1, nw)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, h - 1), np.minimum(x0 + 1, w - 1)
    fy, fx = (ys - y0)[:, None], (xs - x0)[None, :]
    g = gray.astype(np.float32)
    top = g[y0][:, x0] * (1 - fx) + g[y0][:, x1] * fx
    bottom = g[y1][:, x0] * (1 - fx) + g[y1][:, x1] * fx
    img = (top * (1 - fy) + bottom * fy) / 255.0
    img = (img - 0.5) / 0.5
    return np.repeat(img[None, None, :, :], 3, axis=1).astype(np.float32)


def ctc_decode(probs, chars):
    """Greedy CTC: the best class at each step, repeats merged, blanks (class 0) dropped; class k is chars[k - 1]."""
    best = np.asarray(probs).argmax(axis=-1)
    out, prev = [], -1
    for k in best:
        k = int(k)
        if k != prev and k != 0 and k - 1 < len(chars):
            out.append(chars[k - 1])
        prev = k
    return "".join(out)


class OnnxRecogniser:
    """PP-OCRv5 mobile line recognition on onnxruntime: one row image in, its text out."""

    def __init__(self, model, dictionary):
        import onnxruntime
        onnxruntime.disable_telemetry_events()   # as vad.py; the real off switch is ORT_DISABLE_TELEMETRY
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3       # the exported models carry unused initialisers: one warning each on loading
        self.session = onnxruntime.InferenceSession(model, opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        with open(dictionary, encoding="utf-8") as f:
            self.chars = [line.rstrip("\n") for line in f] + [" "]

    def read(self, images):
        return [ctc_decode(self.session.run(None, {self.input: to_input(img)})[0][0], self.chars) for img in images]


_lock = threading.Lock()
_cache = {}
_factory = None


def set_recogniser(factory):
    """Tests: factory(lang) -> a recogniser (anything with read(images)) or None; None puts the model files back."""
    global _factory
    _factory = factory
    forget()


def forget():
    with _lock:
        _cache.clear()


def recogniser(lang):
    """The recogniser for a language, loaded once; None without a model for it or without its files."""
    if _factory is not None:
        return _factory(lang)
    name = model_for(lang)
    if name is None:
        return None
    with _lock:
        if name not in _cache:
            model = os.path.join(MODEL_DIR, f"{name}_rec.onnx")
            dictionary = os.path.join(MODEL_DIR, f"{name}_dict.txt")
            try:
                _cache[name] = OnnxRecogniser(model, dictionary) if os.path.exists(model) and \
                    os.path.exists(dictionary) else None
            except Exception:     # a broken model or no onnxruntime: no words, as without the files
                _cache[name] = None
        return _cache[name]


def texts(codec, payloads, rec, private=b"", budget=BUDGET_S, clock=time.monotonic):
    """{start: text} of image lines {start: block payload}, read by `rec`, within `budget` seconds."""
    out, t0 = {}, clock()
    for s in sorted(payloads):
        if clock() - t0 > budget:
            break
        if codec.startswith("S_VOBSUB"):
            img = vobsub_image(payloads[s], private)
            images = [img] if img is not None else []
        else:
            images = pgs_images(payloads[s])
        lines = [r for img in images for r in rows(img)]
        if not lines:
            continue
        try:
            read = rec.read(lines)
        except Exception:     # the model refused the input (shape, runtime): this line has no words, the sync goes on
            continue
        text = " ".join(t for t in read if t and t.strip())
        if text.strip():
            out[s] = text
    return out
