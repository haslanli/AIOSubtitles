"""The tests' own stand-ins and set-up (helpers.py, imagesubs.py): what other tests rely on them for."""
import ast
import http.client
import os
import unittest

import imagesubs as I
from helpers import RangeServer, TempDir

TESTS = os.path.dirname(os.path.abspath(__file__))


class OnePathSetupTest(unittest.TestCase):
    """Helpers is the one place that puts the package on the import path. Every test module imports it before
    anything from subsync and changes sys.path in no other way, so all of them test the same copy and each one also
    runs on its own."""

    @staticmethod
    def modules():
        for name in sorted(os.listdir(TESTS)):
            if name.startswith("test_") and name.endswith(".py"):
                with open(os.path.join(TESTS, name), encoding="utf-8") as f:
                    yield name, ast.parse(f.read())

    def test_every_test_module_imports_helpers_before_the_package(self):
        for name, tree in self.modules():
            with self.subTest(module=name):
                imported = []                   # the top-level imports' first names, in order
                for node in tree.body:
                    if isinstance(node, ast.Import):
                        imported += [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imported.append((node.module or "").split(".")[0])
                self.assertIn("helpers", imported)
                if "subsync" in imported:
                    self.assertLess(imported.index("helpers"), imported.index("subsync"))

    def test_no_test_module_changes_the_import_path_itself(self):
        for name, tree in self.modules():
            with self.subTest(module=name):
                edits = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "sys.path"]
                self.assertEqual(edits, [])


class RangeServerLogTest(unittest.TestCase):
    """A read is in the log before any of its answer is sent. The log used to get it after the last byte, so a
    client that already had its bytes could look and not find it."""

    def test_a_read_is_logged_before_its_answer_is_sent(self):
        size = 32 << 20                         # far more than the connection's buffers hold: the answer can't all go
        with TempDir() as folder:
            with open(os.path.join(folder, "big.bin"), "wb") as f:
                f.truncate(size)
            srv = RangeServer(folder)
            self.addCleanup(srv.close)
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
            self.addCleanup(conn.close)
            conn.request("GET", "/big.bin", headers={"Range": "bytes=0-"})
            answer = conn.getresponse()
            self.addCleanup(answer.close)
            self.assertEqual((answer.status, len(answer.read(1))), (206, 1))
            self.assertEqual(srv.log, [(0, size)])               # the rest of the answer not sent yet


class PgsShowTest(unittest.TestCase):
    """A split that cuts nothing is refused, so a test meant for an object split over segments can't quietly
    test a whole one (the image fixture's split=900 and test_imagetext's split=300 cut nothing)."""

    def test_a_split_cuts_the_object_into_segments_or_is_refused(self):
        bar = I.bar(700)
        size = len(I.pgs_rle(bar))
        for split in (size, size + 1, 900):
            with self.subTest(split=split), self.assertRaises(ValueError):
                I.pgs_show(bar, split=split)
        segments, p, ds = [], 0, I.pgs_show(bar, split=90)
        while p < len(ds):
            n = int.from_bytes(ds[p + 1:p + 3], "big")
            segments.append(ds[p])
            p += 3 + n
        self.assertEqual(segments.count(0x15), -(-size // 90))   # one object segment per 90 bytes


if __name__ == "__main__":
    unittest.main()
