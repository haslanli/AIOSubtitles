"""In public mode the shared store keeps how a subtitle was synced (its new timings), never its text; each
person's copy is made from the original, downloaded again from its source, with the stored timings. No network."""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import RangeServer, local_network_only  # noqa: E402
from subsync.addon import App, file_id, serve  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.store import Store  # noqa: E402
from subsync.subs import apply_recipe, timing_recipe  # noqa: E402
from subsync.worker import Safety, Worker  # noqa: E402

FILE = "0123456789abcdef-1000"
SRT = ("1\n00:00:01,000 --> 00:00:02,000\nHello <i>there</i>\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n\n"
       "3\n00:00:05,000 --> 00:00:06,000\nYou are a bold one\n")
SYNCED = SRT.replace("00:00:01,000 --> 00:00:02,000", "00:00:01,500 --> 00:00:02,500") \
            .replace("00:00:03,000 --> 00:00:04,000", "00:00:03,500 --> 00:00:04,500") \
            .replace("00:00:05,000 --> 00:00:06,000", "00:00:05,500 --> 00:00:06,500")
ASS = ("[Script Info]\nTitle: x\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi\nDialogue: 0,0:00:03.00,0:00:04.00,Sign,,0,0,0,,{\\pos(1,1)}EXIT\n"
       "Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,Bye\n")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class RecipeTest(unittest.TestCase):
    def test_a_re_timed_srt_is_rebuilt_from_its_original(self):
        r = timing_recipe(SRT, SYNCED)
        self.assertNotIn("Kenobi", repr(r))                     # timings only, no text
        self.assertEqual(apply_recipe(SRT, r), SYNCED)

    def test_an_ass_with_a_dropped_line(self):
        from subsync.subs import parse, write_like
        lines = parse(ASS)
        lines[0] = dict(lines[0], s=1.25, e=2.25)
        lines[1] = dict(lines[1], drop=True)
        body = write_like(ASS, lines)
        r = timing_recipe(ASS, body)
        self.assertEqual(apply_recipe(ASS, r), body)

    def test_unchanged_is_the_original(self):
        self.assertEqual(apply_recipe(SRT, timing_recipe(SRT, SRT)), SRT)

    def test_a_copy_that_is_not_a_re_timing_of_the_original_has_no_recipe(self):
        self.assertIsNone(timing_recipe(SRT, SYNCED.replace("Kenobi", "Grievous")))
        self.assertIsNone(apply_recipe(SRT + "\n4\n00:00:07,000 --> 00:00:08,000\nmore\n", timing_recipe(SRT, SYNCED)))


class TextlessStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def test_no_text_is_written_only_timings(self):
        store = Store(self.dir, keep_texts=False)
        key = store.put(FILE, sha(SRT), "eng", 0, "synced", body=SYNCED, fmt="srt", candidate_id="c1", original=SRT)
        self.assertIsNone(store.body(key))
        self.assertEqual(apply_recipe(SRT, store.recipe(key)), SYNCED)
        found = []
        for root, _, names in os.walk(self.dir):
            for n in names:
                if n.endswith(".gz"):
                    import gzip
                    with gzip.open(os.path.join(root, n), "rt", encoding="utf-8") as f:
                        found.append(f.read())
        self.assertTrue(found and not any("Kenobi" in x for x in found))
        self.assertEqual([r["key"] for r in store.offers(FILE, ["eng"])], [key])
        key2 = store.put(FILE, sha("x"), "eng", 1, "synced", body="nope", fmt="srt", candidate_id="c2")   # no original
        store.set_offers(FILE, "eng", [key2])
        self.assertEqual(store.offers(FILE, ["eng"]), [])                     # nothing to rebuild it from: not offered

    def test_texts_kept_before_public_mode_are_dropped(self):
        old = Store(self.dir)
        key = old.put(FILE, sha(SRT), "eng", 0, "synced", body=SYNCED, fmt="srt", candidate_id="c1")
        self.assertEqual(old.body(key), SYNCED)
        store = Store(self.dir, keep_texts=False)
        old.judged_put(FILE, ["eng"], "rule")
        with self.assertLogs("subsync.store", "INFO") as logs:
            self.assertEqual(store.drop_texts(), 1)
        self.assertIn("1 stored subtitle text dropped (1 row to judge again)", logs.output[0])   # counted before
        self.assertIsNone(store.get(key))
        self.assertFalse(store.judged_all(FILE, ["eng"], "rule", 0))       # judged again on the next play
        self.assertFalse([n for _, _, names in os.walk(os.path.join(self.dir, "bodies")) for n in names])


class ServingTest(unittest.TestCase):
    """A person's copy is made from the original their own list gives, downloaded again, with the stored timings."""

    def setUp(self):
        self.guard = local_network_only()
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        os.makedirs(os.path.join(self.dir, "subs"))
        with open(os.path.join(self.dir, "subs", "orig.srt"), "w", encoding="utf-8") as f:
            f.write(SRT)
        self.srv = RangeServer(os.path.join(self.dir, "subs"))
        self.addCleanup(self.srv.close)
        url = self.srv.url("orig.srt")

        class Listing:
            config_sources = set()

            def subtitles(self, *a):
                return [{"id": "c1", "url": url, "lang": "eng"}]

            def find_row(self, *a):
                return None

        self.store = Store(os.path.join(self.dir, "store"), keep_texts=False)
        self.key = self.store.put(FILE, sha(SRT), "eng", 0, "synced", body=SYNCED, fmt="srt", candidate_id="c1",
                                  original=SRT)
        self.store.alias("name:a.mkv|1000", FILE)
        s = Settings({"SUBSYNC_DATA": os.path.join(self.dir, "store"), "SUBSYNC_PUBLIC_BASE": "http://x"})
        self.worker = Worker(s, self.store, Listing(), Safety())
        self.worker.seen["name:a.mkv|1000"] = ("movie", "tt1", {"filename": "a.mkv", "videoSize": 1000})
        self.server = serve(App(s, self.store, self.worker, Safety()), 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_the_copy_is_rebuilt_from_a_fresh_download(self):
        code, body = self.get(f"/file/{file_id(self.key)}.srt")
        self.assertEqual((code, body), (200, SYNCED))
        self.assertTrue(self.srv.log)                                         # the original was downloaded

    def test_a_changed_original_is_not_served_with_old_timings(self):
        with open(os.path.join(self.dir, "subs", "orig.srt"), "w", encoding="utf-8") as f:
            f.write(SRT.replace("Kenobi", "Grievous"))
        code, _ = self.get(f"/file/{file_id(self.key)}.srt")
        self.assertEqual(code, 404)


class SignupCleanUpTest(unittest.TestCase):
    def test_the_twins_of_the_built_in_sources_are_removed_on_request(self):
        from subsync.aiosconfig import viewer_change
        presets = [{"type": "torrentio", "instanceId": "t", "enabled": True, "options": {"name": "Torrentio"}},
                   {"type": "opensubtitles", "instanceId": "o", "enabled": True,
                    "options": {"name": "OpenSubtitles", "resources": ["subtitles"]}},
                   {"type": "opensubtitles-v3-plus", "instanceId": "p", "enabled": True, "options": {"name": "OS V3+"}},
                   {"type": "subdl", "instanceId": "d", "enabled": False, "options": {"name": "SubDL"}},
                   {"type": "subsource", "instanceId": "s", "enabled": True, "options": {"name": "SubSource"}},
                   {"type": "subhero", "instanceId": "h", "enabled": True, "options": {"name": "SubHero"}}]
        new, changes = viewer_change({"presets": presets}, "https://a.example/u/t/manifest.json", clean_up=True)
        types = [p["type"] for p in new["presets"]]
        self.assertEqual(types, ["torrentio", "custom", "subhero"])           # Wyzie isn't asked by the public service
        self.assertTrue(any("removes 4 subtitle addons" in c for c in changes), changes)
        new, _ = viewer_change({"presets": presets}, "https://a.example/u/t/manifest.json")
        self.assertEqual(len(new["presets"]), len(presets) + 1)               # not asked: nothing removed


if __name__ == "__main__":
    unittest.main()
