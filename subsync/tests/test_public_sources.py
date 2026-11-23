"""In public mode the shared store keeps how a subtitle was synced (its new timings), never its text; each
person's copy is made from the original, downloaded again from its source, with the stored timings. No network."""
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request

from helpers import closed_after, local_network_only, RangeServer, start_server, stop_server
from subsync import srclimits
from subsync.addon import App, file_id, serve
from subsync.settings import Settings
from subsync.store import Store
from subsync.subs import (apply_recipe, fingerprint, line_positions, parse, recognised,
                          timing_recipe, write_like)
from subsync.worker import Safety, Worker

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

    def test_an_ass_with_a_dropped_line_whose_text_another_layer_repeats(self):
        from subsync.subs import parse, write_like
        original = ASS.replace("Dialogue: 0,0:00:03.00,0:00:04.00,Sign,,0,0,0,,{\\pos(1,1)}EXIT",
                               "Dialogue: 1,0:00:01.50,0:00:02.50,Default,,0,0,0,,Hi")   # "Hi" again, on layer 1
        lines = parse(original)
        lines[0] = dict(lines[0], drop=True)
        lines[1] = dict(lines[1], s=2.0, e=3.0)
        body = write_like(original, lines)
        r = timing_recipe(original, body)                    # the copy's "Hi" is layer 1's, not the dropped one's
        self.assertIsNotNone(r)
        self.assertEqual(r["times"], [None, [2000, 3000], [5000, 6000]])
        self.assertEqual(apply_recipe(original, r), body)

    def test_unchanged_is_the_original(self):
        self.assertEqual(apply_recipe(SRT, timing_recipe(SRT, SRT)), SRT)

    def test_a_changed_original_is_recognised_by_its_words_timings_and_line_positions(self):
        # The words and timings leave credit lines out, so an original whose credit line moved from the end
        # to the start was still recognised
        credit = "Subtitles downloaded from www.example.org"
        original = SRT + f"\n4\n00:00:59,000 --> 00:01:00,000\n{credit}\n"
        moved = f"1\n00:00:00,000 --> 00:00:00,500\n{credit}\n\n" + SRT
        recipe = {"v": 1, "times": [], "fp": fingerprint(original), "positions": line_positions(original)}
        self.assertTrue(recognised(original.replace("\n", "\r\n"), recipe))     # other bytes, the same subtitle
        self.assertEqual(fingerprint(moved), recipe["fp"])                       # its words and timings stayed
        self.assertFalse(recognised(moved, recipe))
        self.assertFalse(recognised(original.replace("Kenobi", "Grievous"), recipe))
        self.assertFalse(recognised(original, dict(recipe, positions=None)))     # a recipe kept before

    def test_a_recipe_of_another_form_makes_no_copy(self):
        # The recipe's form ("v") was written but never read, so a recipe a later version kept (a rollback
        # runs this version on the data as it is) was applied as this version's
        from subsync.subs import RECIPE_VERSION
        for recipe in (timing_recipe(SRT, SYNCED), timing_recipe(SRT, SRT)):
            self.assertEqual(recipe["v"], RECIPE_VERSION)
            self.assertIsNotNone(apply_recipe(SRT, recipe))
            for other in (dict(recipe, v=RECIPE_VERSION + 1), {k: v for k, v in recipe.items() if k != "v"}):
                self.assertIsNone(apply_recipe(SRT, other))

    def test_a_copy_that_is_not_a_re_timing_of_the_original_has_no_recipe(self):
        self.assertIsNone(timing_recipe(SRT, SYNCED.replace("Kenobi", "Grievous")))
        self.assertIsNone(apply_recipe(SRT + "\n4\n00:00:07,000 --> 00:00:08,000\nmore\n", timing_recipe(SRT, SYNCED)))


class TextlessStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)

    def test_no_text_is_written_only_timings(self):
        store = closed_after(self, Store(self.dir, keep_texts=False))
        key = store.put(FILE, sha(SRT), "eng", 0, "synced", body=SYNCED, fmt="srt", candidate_id="c1", original=SRT)
        self.assertIsNone(store.body(key))
        self.assertEqual(apply_recipe(SRT, store.recipe(key)), SYNCED)
        # every file the store wrote, the database and its journal included (only the .gz files were read),
        # holds none of the subtitle's lines; the recipe, gzipped, is one of them
        files = {}
        for root, _, names in os.walk(self.dir):
            for n in names:
                with open(os.path.join(root, n), "rb") as f:
                    files[n] = gzip.decompress(f.read()) if n.endswith(".gz") else f.read()
        self.assertTrue([n for n in files if n.endswith(".gz")], sorted(files))
        for line in (b"Hello", b"Kenobi", b"bold one"):
            with self.subTest(line=line):
                self.assertEqual([n for n, data in files.items() if line in data], [])
        self.assertEqual([r["key"] for r in store.offers(FILE, ["eng"])], [key])
        key2 = store.put(FILE, sha("x"), "eng", 1, "synced", body="nope", fmt="srt", candidate_id="c2")   # no original
        store.set_offers(FILE, "eng", [key2])
        self.assertEqual(store.offers(FILE, ["eng"]), [])                     # nothing to rebuild it from: not offered

    def test_texts_kept_before_public_mode_are_dropped(self):
        old = closed_after(self, Store(self.dir))
        key = old.put(FILE, sha(SRT), "eng", 0, "synced", body=SYNCED, fmt="srt", candidate_id="c1")
        self.assertEqual(old.body(key), SYNCED)
        store = closed_after(self, Store(self.dir, keep_texts=False))
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

        self.store = closed_after(self, Store(os.path.join(self.dir, "store"), keep_texts=False))
        self.key = self.store.put(FILE, sha(SRT), "eng", 0, "synced", body=SYNCED, fmt="srt", candidate_id="c1",
                                  original=SRT)
        self.store.alias("name:a.mkv|1000", FILE)
        s = Settings({"SUBSYNC_DATA": os.path.join(self.dir, "store"), "SUBSYNC_PUBLIC_BASE": "http://x"})
        self.worker = Worker(s, self.store, Listing(), Safety())
        self.worker.seen["name:a.mkv|1000"] = ("movie", "tt1", {"filename": "a.mkv", "videoSize": 1000})
        self.server = start_server(serve(App(s, self.store, self.worker, Safety()), 0))
        self.addCleanup(stop_server, self.server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            e.close()                            # its answer let go at once
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

    def put_original(self, text):
        """`text` as the source serves it now, downloaded again on the next request."""
        with open(os.path.join(self.dir, "subs", "orig.srt"), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.worker.fetched_subs.clear()

    def stored(self, original):
        """A copy of `original` a second later, stored as public mode stores it: (its key, its text)."""
        body = write_like(original, [dict(l, s=l["s"] + 1, e=l["e"] + 1) for l in parse(original)])
        key = self.store.put(FILE, sha(original), "eng", 0, "synced", body=body, fmt="srt", candidate_id="c1",
                             original=original)
        return key, body

    def test_a_changed_original_whose_lines_moved_is_not_served(self):
        # The site moved a credit line from the end to the start; the words and timings the original is
        # recognised by, and its number of lines, stayed as they were, so the copy was rebuilt one line off throughout
        credit = "Subtitles downloaded from www.example.org"
        original = SRT + f"\n4\n00:00:59,000 --> 00:01:00,000\n{credit}\n"
        key, body = self.stored(original)
        self.assertNotIn("Kenobi", json.dumps(self.store.recipe(key)))       # where its lines were: times only
        self.put_original(f"1\n00:00:00,000 --> 00:00:00,500\n{credit}\n\n" + SRT)
        self.assertEqual(self.get(f"/file/{file_id(key)}.srt")[0], 404)

    def test_a_changed_original_with_its_lines_where_they_were_is_served(self):
        key, body = self.stored(SRT)
        self.put_original(SRT.replace("\n", "\r\n"))                          # other bytes, the same lines
        self.assertEqual(self.get(f"/file/{file_id(key)}.srt"), (200, body))

    def test_a_copy_kept_without_line_positions_is_served_only_from_its_unchanged_original(self):
        # A recipe kept before has no check of where its original's lines were
        key, body = self.stored(SRT)
        path = self.store._recipe_path(key)
        with gzip.open(path, "rt", encoding="utf-8") as f:
            recipe = json.load(f)
        del recipe["positions"]
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(recipe, f)
        self.put_original(SRT.replace("\n", "\r\n"))
        self.assertEqual(self.get(f"/file/{file_id(key)}.srt")[0], 404)
        self.put_original(SRT)
        self.assertEqual(self.get(f"/file/{file_id(key)}.srt"), (200, body))


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
        new, changes = viewer_change({"presets": presets}, "https://a.example/u/t/manifest.json",
                                     clean_up=srclimits.CLEANABLE_PRESETS)
        types = [p["type"] for p in new["presets"]]
        self.assertEqual(types, ["torrentio", "custom", "subhero"])           # Wyzie isn't asked by the public service
        self.assertTrue(any("removes 4 subtitle addons" in c for c in changes), changes)
        new, _ = viewer_change({"presets": presets}, "https://a.example/u/t/manifest.json")
        self.assertEqual(len(new["presets"]), len(presets) + 1)               # not asked: nothing removed
        # Only the presets given, those whose site the instance asks itself
        new, changes = viewer_change({"presets": presets}, "https://a.example/u/t/manifest.json",
                                     clean_up=("opensubtitles", "opensubtitles-v3-plus"))
        self.assertEqual([p["type"] for p in new["presets"]], ["torrentio", "custom", "subdl", "subsource", "subhero"])
        self.assertTrue(any("removes 2 subtitle addons" in c for c in changes), changes)

    def test_the_clean_up_takes_only_the_addons_whose_site_the_instance_asks(self):
        # SubDL and SubSource only with their key, the OpenSubtitles ones while SUBSYNC_EXTRA_SOURCES lists
        # them, never Wyzie; the box is left out when none is left
        from subsync.extra import cleanable_presets
        public = {"SUBSYNC_PUBLIC": "on"}
        cases = (({}, ("opensubtitles", "opensubtitles-v3-plus")),
                 ({"SUBSYNC_SUBDL_KEY": "k"}, ("opensubtitles", "opensubtitles-v3-plus", "subdl")),
                 ({"SUBSYNC_SUBDL_KEY": "k", "SUBSYNC_SUBSOURCE_KEY": "k"}, srclimits.CLEANABLE_PRESETS),
                 ({"SUBSYNC_EXTRA_SOURCES": "osv3plus,tsdb"}, ("opensubtitles-v3-plus",)),
                 ({"SUBSYNC_EXTRA_SOURCES": "tsdb,gestdown"}, ()),
                 ({"SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_SUBSOURCE_KEY": "k"}, ("subsource",)))
        for env, presets in cases:
            with self.subTest(env=env):
                self.assertEqual(cleanable_presets(Settings(dict(public, **env))), presets)


if __name__ == "__main__":
    unittest.main()
