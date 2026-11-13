"""Words on image subtitle tracks: PGS and VobSub decoded here, rows cut, read by a recogniser; no model in these
tests (a fake recogniser reads a bar's width as its text), no network."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import imagesubs as I  # noqa: E402
from subsync import imagetext, mkv, pipeline, textpair  # noqa: E402
from subsync.reader import ReadFailed, Refused  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
WORDS = ("time money house water night light dream river stone heart road friend father mother brother sister city "
         "story letter window garden forest winter summer morning evening doctor captain soldier machine").split()


def text_of_width(w):
    """The test's words for a bar of width w (the fake recogniser's 'reading')."""
    r = random.Random(w)
    return " ".join(r.choice(WORDS) for _ in range(5))


class FakeRecogniser:
    """Reads a line image's width as its text; counts the lines it was given."""

    def __init__(self):
        self.lines = 0

    def read(self, images):
        self.lines += len(images)
        return [text_of_width(img.shape[1]) for img in images]


class Bytes:
    def __init__(self, path, refuse=False):
        with open(path, "rb") as f:
            self.b = f.read()
        self.size, self.reads, self.refuse = len(self.b), [], refuse
        self.fail = None      # fail(n) -> True: the n-th read (from 0) fails as a dead connection does (ReadFailed)

    def get(self, start, length):
        self.reads.append(length)
        if self.refuse:
            raise Refused("HTTP 403")
        if self.fail and self.fail(len(self.reads) - 1):
            raise ReadFailed("URLError")
        return self.b[start:start + length]


def ink(img):
    return img < 128


class PgsDecodeTest(unittest.TestCase):
    def test_a_bar_comes_back_with_its_fill_dark_and_its_ground_white(self):
        bar = I.bar(80)
        objs = imagetext.pgs_images(I.pgs_show(bar))
        self.assertEqual(len(objs), 1)
        img = objs[0]
        self.assertEqual(img.shape, bar.shape)
        self.assertTrue(ink(img)[bar == 1].all())               # white fill -> dark ink, as text on paper
        self.assertFalse(ink(img)[bar != 1].any())             # outline and ground light

    def test_long_runs_and_an_object_split_over_segments(self):
        bar = I.bar(700)
        a = imagetext.pgs_images(I.pgs_show(bar))[0]
        b = imagetext.pgs_images(I.pgs_show(bar, split=300))[0]
        self.assertEqual(a.shape, (24, 700))
        np.testing.assert_array_equal(a, b)

    def test_a_clear_has_no_image(self):
        self.assertEqual(imagetext.pgs_images(I.pgs_clear()), [])

    def test_damaged_data_gives_nothing_rather_than_raising(self):
        ds = I.pgs_show(I.bar(80))
        self.assertEqual(imagetext.pgs_images(ds[:40]), [])
        self.assertEqual(imagetext.pgs_images(b"\x15\xff\xff"), [])


class VobSubDecodeTest(unittest.TestCase):
    def test_a_bar_comes_back_from_its_two_fields(self):
        bar = I.bar(150, height=20)
        img = imagetext.vobsub_image(I.vobsub_spu(bar), I.vobsub_private())
        self.assertEqual(img.shape, bar.shape)
        self.assertTrue(ink(img)[bar == 1].all())
        self.assertFalse(ink(img)[bar != 1].any())

    def test_the_palette_comes_from_the_tracks_private_data(self):
        self.assertEqual(len(imagetext.vobsub_palette(I.vobsub_private())), 16)
        self.assertIsNone(imagetext.vobsub_image(b"\x00", I.vobsub_private()))


class RowsTest(unittest.TestCase):
    def test_two_rows_are_cut_apart(self):
        img = imagetext.pgs_images(I.pgs_show(I.stack(I.bar(120), I.bar(200))))[0]
        rows = imagetext.rows(img)
        self.assertEqual([r.shape[1] for r in rows], [img.shape[1], img.shape[1]])
        self.assertEqual(len(rows), 2)

    def test_an_empty_image_has_no_rows(self):
        self.assertEqual(imagetext.rows(np.full((30, 50), 255, dtype=np.uint8)), [])

    def test_input_for_the_recogniser(self):
        x = imagetext.to_input(np.full((24, 96), 255, dtype=np.uint8))
        self.assertEqual(x.shape, (1, 3, 48, 192))
        self.assertAlmostEqual(float(x.max()), 1.0)
        self.assertEqual(x.dtype, np.float32)


class CtcTest(unittest.TestCase):
    def test_greedy_decoding_drops_blanks_and_repeats(self):
        chars = ["a", "b", "c", " "]
        seq = [1, 1, 0, 2, 0, 2, 4, 4, 1, 0]          # a a _ b _ b ' ' ' ' a _  (0 = blank, 4 = ' ')
        probs = np.eye(5)[seq]
        self.assertEqual(imagetext.ctc_decode(probs, chars), "abb a")


class ModelChoiceTest(unittest.TestCase):
    def test_latin_or_eastern_slavic_by_language(self):
        self.assertEqual(imagetext.model_for("eng"), "latin")
        self.assertEqual(imagetext.model_for("fr"), "latin")
        self.assertEqual(imagetext.model_for("rus"), "eslav")
        self.assertEqual(imagetext.model_for("uk"), "eslav")
        self.assertIsNone(imagetext.model_for("jpn"))

    def test_no_model_files_no_recogniser(self):
        old = imagetext.MODEL_DIR
        try:
            imagetext.MODEL_DIR = os.path.join(FIXTURES, "no-such-folder")
            imagetext.forget()
            self.assertIsNone(imagetext.recogniser("eng"))
        finally:
            imagetext.MODEL_DIR = old
            imagetext.forget()


class TrackImagesReadTest(unittest.TestCase):
    """Each sampled line's display set read from its block place: 2 KB, then the rest, at most 64 KB."""

    def index(self, name="tiny-pgs.mkv", **kw):
        r = Bytes(os.path.join(FIXTURES, name), **kw)
        return r, mkv.MkvIndex(r).read_head().read_cues()

    def starts(self, index):
        t = next(t for t in index.image_tracks())
        return t, [s for s, _ in sorted(t["cues"])][0::2]          # the display sets that show a line

    def test_display_sets_read_and_decoded_to_their_bars(self):
        for name in ("tiny-pgs.mkv", "tiny-pgs-zlib.mkv"):
            r, index = self.index(name)
            t, starts = self.starts(index)
            got = index.line_images(t["number"], starts)
            widths = [imagetext.pgs_images(got[s])[0].shape[1] for s in starts]
            self.assertEqual(widths[:4] + widths[5:], [60 + 4 * i for i in range(30) if i != 4], name)

    def test_a_small_block_takes_one_read(self):
        r, index = self.index()
        t, starts = self.starts(index)
        before = len(r.reads)
        index.line_images(t["number"], starts[:3])
        self.assertEqual(r.reads[before:], [mkv.WORD_READ] * 3)

    def test_a_larger_block_takes_a_second_read_for_the_rest(self):
        r, index = self.index()
        t, starts = self.starts(index)
        old = mkv.WORD_READ
        try:
            mkv.WORD_READ = 100
            before = len(r.reads)
            got = index.line_images(t["number"], starts[:3])
        finally:
            mkv.WORD_READ = old
        self.assertEqual(len(r.reads) - before, 6)
        self.assertEqual([imagetext.pgs_images(got[s])[0].shape[1] for s in starts[:3]], [60, 64, 68])

    def test_a_block_over_the_cap_is_skipped(self):
        r, index = self.index()
        t, starts = self.starts(index)
        old = mkv.IMAGE_READ
        try:
            mkv.IMAGE_READ = 200
            before = len(r.reads)
            got = index.line_images(t["number"], starts[:6])
            self.assertLess(len(got), 6)
            self.assertTrue(all(n <= 200 for n in r.reads[before:]))
        finally:
            mkv.IMAGE_READ = old

    def test_a_refusal_stops_the_reads(self):
        r, index = self.index()
        t, starts = self.starts(index)
        r.refuse, before = True, len(r.reads)
        with self.assertRaises(Refused):
            index.line_images(t["number"], starts)
        self.assertLessEqual(len(r.reads) - before, mkv.WORD_READS_AT_ONCE + 1)

    def test_reads_stop_after_three_failures_in_a_row(self):
        r, index = self.index()
        t, starts = self.starts(index)
        old = mkv.WORD_READS_AT_ONCE
        try:
            mkv.WORD_READS_AT_ONCE = 1                      # one at a time: the count is exact
            before = len(r.reads)
            r.fail = lambda n: n >= before
            self.assertEqual(index.line_images(t["number"], starts), {})
        finally:
            mkv.WORD_READS_AT_ONCE = old
        self.assertEqual(len(r.reads) - before, mkv.MAX_FAILED_IN_A_ROW)


class Index:
    """A file whose only reference is an English PGS track; its lines' images come from imagesubs."""

    def __init__(self, cues):
        self.cues = cues
        self.asked = []
        self.widths = {s: 60 + 2 * i for i, (s, _) in enumerate(cues)}

    def text_tracks(self):
        return []

    def image_tracks(self):
        events = []
        for s, d in self.cues:
            events += [(s, 0.0), (s + d, 0.0)]
        return [{"number": 4, "language": "en", "codec": "S_HDMV/PGS", "name": "", "forced": False, "cues": events}]

    def line_images(self, number, starts):
        self.asked.append(list(starts))
        return {s: I.pgs_show(I.bar(self.widths[s])) for s in starts if s in self.widths}

    def codec_private(self, number):
        return b""


def film(n=400, seed=3):
    """Lines as a PGS track times them: short gaps between lines of one exchange and long pauses between scenes, which
    let imageref tell the display sets that show a line from those that clear one."""
    r = random.Random(seed)
    t, out = 20.0, []
    for _ in range(n):
        d = r.uniform(1.0, 3.5)
        out.append((round(t, 3), d))
        k = r.random()
        t += d + (r.uniform(0.08, 0.2) if k < 0.5 else r.uniform(9.0, 14.0) if k < 0.7 else r.uniform(0.6, 2.5))
    return out


def srt(lines):
    from subsync.subs import ts
    return "\n".join(f"{i}\n{ts(s)} --> {ts(e)}\n{t}\n" for i, (s, e, t) in enumerate(lines, 1))


class ImageWordsPipelineTest(unittest.TestCase):
    """An image track in the subtitle's language gives words too: read only when D1 allows, D2 as on text."""

    def setUp(self):
        self.fake = FakeRecogniser()
        imagetext.set_recogniser(lambda lang: self.fake if imagetext.model_for(lang) else None)

    def tearDown(self):
        imagetext.set_recogniser(None)

    def setup_film(self):
        cues = film()
        index = Index(cues)
        text = {s: text_of_width(index.widths[s]) for s, _ in cues}
        ref = pipeline.choose_reference(index, len(cues), "eng", image_ref=True)
        self.assertEqual(ref["route"], "image")
        return cues, index, text, ref

    def test_the_image_tracks_words_are_read_and_pair(self):
        cues, index, text, ref = self.setup_film()
        track = pipeline.track_words(index, ref, True)
        self.assertIsNotNone(track)
        self.assertLessEqual(sum(len(a) for a in index.asked), textpair.MAX_LINES)
        self.assertEqual(self.fake.lines, len(track))
        lines = [{"s": s - 30.0, "e": s - 30.0 + d, "text": text[s], "kind": "dialogue"} for s, d in cues]
        self.assertGreaterEqual(len(textpair.match(lines, track)), textpair.MIN_ANCHORS)

    def test_words_that_disagree_refuse_a_pass_on_an_image_track(self):
        cues, index, text, ref = self.setup_film()
        pipeline.track_words(index, ref, True)
        wrong = [(cues[i][0], cues[i][0] + cues[i][1], text[cues[i + 1][0]]) for i in range(len(cues) - 1)]
        chk = pipeline.check_original(index, srt(wrong), "eng", image_ref=True, text_pairing=True)
        self.assertIsNotNone(chk)
        self.assertIn("text", chk)
        self.assertFalse(chk["text"]["ok"])
        right = [(s, s + d, text[s]) for s, d in cues]
        chk = pipeline.check_original(index, srt(right), "eng", image_ref=True, text_pairing=True)
        self.assertTrue(chk["text"]["ok"])

    def test_nothing_is_read_without_a_recogniser_for_the_language(self):
        imagetext.set_recogniser(lambda lang: None)
        cues, index, text, ref = self.setup_film()
        self.assertIsNone(pipeline.track_words(index, ref, True))
        self.assertEqual(index.asked, [])

    def test_image_lines_are_read_a_batch_at_a_time_within_the_time_budget(self):
        cues, index, text, ref = self.setup_film()
        pipeline.track_words(index, ref, True)
        self.assertGreater(len(index.asked), 1)
        self.assertTrue(all(len(a) <= pipeline.IMAGE_BATCH for a in index.asked))
        cues, index, text, ref = self.setup_film()
        old = imagetext.BUDGET_S
        try:
            imagetext.BUDGET_S = -1.0                   # no time left: nothing more is read
            self.assertIsNone(pipeline.track_words(index, ref, True))
            self.assertEqual(index.asked, [])
        finally:
            imagetext.BUDGET_S = old

    def test_a_recogniser_that_fails_gives_no_words_not_a_failed_sync(self):
        class Broken:
            def read(self, images):
                raise RuntimeError("model refused the input")
        imagetext.set_recogniser(lambda lang: Broken())
        cues, index, text, ref = self.setup_film()
        self.assertIsNone(pipeline.track_words(index, ref, True))

    def test_nothing_is_read_while_not_allowed(self):
        cues, index, text, ref = self.setup_film()
        self.assertIsNone(pipeline.track_words(index, ref, False))
        self.assertEqual(index.asked, [])


def _models_here():
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return all(os.path.exists(os.path.join(imagetext.MODEL_DIR, f)) for f in ("latin_rec.onnx", "eslav_rec.onnx"))


DOCKERFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Dockerfile")


class OcrTelemetryTest(unittest.TestCase):
    """Like vad.py, the recogniser turns onnxruntime's usage events off before it makes a session (onnxruntime
    1.31 sends them to Microsoft from Linux too), so the public server makes no connection the privacy page doesn't
    name. A stand-in onnxruntime records the calls; no model needed."""

    def test_usage_events_are_off_before_any_session(self):
        import types
        calls = []

        class Session:
            def __init__(self, *a, **kw):
                calls.append("session")

            def get_inputs(self):
                return [types.SimpleNamespace(name="x")]

        fake = types.ModuleType("onnxruntime")
        fake.disable_telemetry_events = lambda: calls.append("telemetry off")
        fake.SessionOptions = lambda: types.SimpleNamespace()
        fake.InferenceSession = Session
        old = sys.modules.get("onnxruntime")
        sys.modules["onnxruntime"] = fake
        try:
            imagetext.OnnxRecogniser("model.onnx", os.path.join(imagetext.MODEL_DIR, "latin_dict.txt"))
        finally:
            if old is None:
                sys.modules.pop("onnxruntime", None)
            else:
                sys.modules["onnxruntime"] = old
        self.assertEqual(calls, ["telemetry off", "session"])

    def test_the_package_sets_the_runtime_s_off_switch_before_it_loads(self):
        # disable_telemetry_events alone left a connection to Microsoft's collector on the box; only
        # ORT_DISABLE_TELEMETRY, set before onnxruntime loads, stopped it. Importing subsync sets it
        import subprocess
        env = dict(os.environ, ORT_DISABLE_TELEMETRY="0")              # inherited the other way, as an env file might
        root = os.path.dirname(os.path.dirname(imagetext.__file__))
        out = subprocess.run([sys.executable, "-c", "import os, subsync; print(os.environ['ORT_DISABLE_TELEMETRY'])"],
                             cwd=root, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "1", out.stderr)

    @unittest.skipUnless(os.path.exists(DOCKERFILE), "the Dockerfile is in the source, not in the image")
    def test_the_image_sets_the_runtime_s_off_switch(self):
        with open(DOCKERFILE, encoding="utf-8") as f:
            self.assertIn("\nENV ORT_DISABLE_TELEMETRY=1\n", f.read())


class OcrCharacterListsTest(unittest.TestCase):
    """The two models' character lists ship with the source (the models themselves are fetched by the image build)."""

    def test_both_lists_are_there(self):
        for name, letter in (("latin", "é"), ("eslav", "ж")):
            with open(os.path.join(imagetext.MODEL_DIR, f"{name}_dict.txt"), encoding="utf-8") as f:
                chars = [line.rstrip("\n") for line in f]
            self.assertGreater(len(chars), 100)
            self.assertIn(letter, chars)
            self.assertIn("A" if name == "latin" else "Д", chars)


@unittest.skipUnless(_models_here(), "the PP-OCRv5 models are put in by the image build (Dockerfile)")
class OcrModelTest(unittest.TestCase):
    """In the image: the models read rendered subtitle rows (our own sentences, decoded from PGS display sets)."""

    def setUp(self):
        imagetext.set_recogniser(None)

    def test_each_models_classes_are_its_characters_and_the_blank(self):
        for lang in ("eng", "rus"):
            rec = imagetext.recogniser(lang)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.session.get_outputs()[0].shape[-1], len(rec.chars) + 1)

    def test_rendered_rows_are_read(self):
        fx = np.load(os.path.join(FIXTURES, "ocr-lines.npz"))
        for lang in ("eng", "rus"):
            keys = sorted(k for k in fx.files if k.startswith(lang + "_") and not k.endswith("_text"))
            rec = imagetext.recogniser(lang)
            ok = 0
            for k in keys:
                truth = bytes(fx[k + "_text"]).decode("utf-8")
                read = rec.read([fx[k]])[0]
                a, b = textpair.pairs_of(truth)[0], textpair.pairs_of(read)[0]
                ok += bool(a and b) and len(a & b) >= textpair.SAME * max(len(a), len(b))
            self.assertGreaterEqual(ok, len(keys) - 1, lang)


if __name__ == "__main__":
    unittest.main()
