"""The tests' own stand-ins and set-up (helpers.py, imagesubs.py): what other tests rely on them for."""
import ast
import http.client
import logging
import os
import shutil
import socket
import subprocess
import sys
import unittest
import urllib.request
from unittest import mock

import imagesubs as I
from helpers import local_lookup, RangeServer, SOURCE, TempDir, watch_the_log
from subsync import net

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

    def test_a_module_also_runs_from_the_package_folder(self):
        # `python3 -m unittest tests.test_x` in subsync/ (the folder above this one) finds helpers too
        env = {k: v for k, v in os.environ.items() if k != "AIOSUBTITLES_IMAGE_BUILD"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        out = subprocess.run([sys.executable, "-m", "unittest", "tests.test_net"], cwd=SOURCE, env=env,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertRegex(out.stderr, r"Ran [1-9]\d* tests")


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


class ImageBuildTest(unittest.TestCase):
    """The image build tests the image's own copy of the package, the one that holds the OCR models the build
    adds, and fails without them. Its tests imported the source's copy (no models), so the model tests were skipped in
    every build. Run here as the Dockerfile runs them: from a folder that holds a copy of the package, with
    AIOSUBTITLES_IMAGE_BUILD set; and as before without it."""

    def setUp(self):
        tmp = TempDir()
        self.image = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        shutil.copytree(os.path.join(SOURCE, "subsync"), os.path.join(self.image, "subsync"),
                        ignore=shutil.ignore_patterns("__pycache__"))

    def python(self, image_build, *args):
        env = {k: v for k, v in os.environ.items() if k != "AIOSUBTITLES_IMAGE_BUILD"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (TESTS, env.get("PYTHONPATH"))))   # helpers, as -t gives it
        if image_build:
            env["AIOSUBTITLES_IMAGE_BUILD"] = "1"
        return subprocess.run([sys.executable, *args], cwd=self.image, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=300)

    def test_the_image_build_tests_the_images_own_copy(self):
        for image_build, folder in ((True, self.image), (False, SOURCE)):
            with self.subTest(image_build=image_build):
                out = self.python(image_build, "-c", "import helpers, subsync; print(subsync.__file__)")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(os.path.realpath(out.stdout.strip()),
                                 os.path.realpath(os.path.join(folder, "subsync", "__init__.py")))

    def test_the_image_build_fails_without_the_ocr_models(self):
        # the copy has no models (the source has none): skipped outside the image build, a failure inside it
        for image_build, code, summary in ((True, 1, "FAILED (failures=2)"), (False, 0, "OK (skipped=2)")):
            with self.subTest(image_build=image_build):
                out = self.python(image_build, "-m", "unittest", "discover", "-s", TESTS, "-t", TESTS,
                                  "-p", "test_imagetext.py", "-k", "OcrModelTest")
                self.assertEqual(out.returncode, code, out.stderr)
                self.assertIn("Ran 2 tests", out.stderr)
                self.assertIn(summary, out.stderr)


class WatchTheLogTest(unittest.TestCase):
    """watch_the_log fails a test whose run put a link in subsync's log (any subsync logger, DEBUG and up, in
    the message or its exception), passes one that didn't, and leaves logging as it found it."""

    @staticmethod
    def outcome(log):
        class Inner(unittest.TestCase):
            def test(inner):
                watch_the_log(inner)
                log()
        result = unittest.TestResult()
        Inner("test").run(result)
        return result

    def test_a_link_in_the_log_fails_the_test(self):
        factory, level = logging.getLogRecordFactory(), logging.getLogger("subsync").level
        link = "http://127.0.0.1:8790/f.mkv"
        leaks = {"debug": lambda: logging.getLogger("subsync.worker").debug("read %s", link),
                 "message": lambda: logging.getLogger("subsync").info("failed: %s", RuntimeError(link)),
                 "exception": lambda: logging.getLogger("subsync.read").info("failed", exc_info=RuntimeError(link)),
                 "aiostreams path": lambda: logging.getLogger("subsync.sources").debug("asked /stremio/u/e/stream")}
        for name, log in leaks.items():
            with self.subTest(leak=name):
                self.assertEqual(len(self.outcome(log).failures), 1)
        clean = self.outcome(lambda: logging.getLogger("subsync.worker").info("read failed movie/tt1 (URLError)"))
        self.assertTrue(clean.wasSuccessful())
        self.assertIs(logging.getLogRecordFactory(), factory)
        self.assertEqual(logging.getLogger("subsync").level, level)


class LocalLookupTest(unittest.TestCase):
    """For the whole run, only this machine's names are looked up, so a test that reaches for another machine
    fails before any lookup, whatever settings it leaves at their defaults, and every module keeps its "no network".
    No connection elsewhere is even tried here (the review's runner counts every one)."""

    def test_another_machine_is_never_looked_up(self):
        self.assertIs(socket.getaddrinfo, local_lookup)
        for host in ("opensubtitles-v3.strem.io", b"query.wikidata.org", "93.184.216.34", "2606:4700:4700::1111"):
            with self.subTest(host=host), self.assertRaises(AssertionError):
                socket.getaddrinfo(host, 443)

    def test_every_connection_looks_its_host_up_through_it(self):
        # socket.create_connection, so urllib too, takes the socket module's lookup when it connects: a connection to
        # another machine fails at that lookup
        server = RangeServer(TESTS)
        self.addCleanup(server.close)
        asked, lookup = [], socket.getaddrinfo

        def spy(host, *a, **k):
            asked.append(host)
            return lookup(host, *a, **k)
        with mock.patch.object(socket, "getaddrinfo", spy):
            socket.create_connection(("127.0.0.1", server.port), timeout=5).close()
            with urllib.request.urlopen(server.url("helpers.py"), timeout=5) as r:
                self.assertEqual(r.status, 200)
        self.assertEqual(asked, ["127.0.0.1", "127.0.0.1"])

    def test_nor_does_the_gate_of_public_mode(self):
        net.configure(public_only=True)                     # its own resolver: the socket module's, as main_public
        self.addCleanup(net.configure, public_only=False)
        with self.assertRaises(AssertionError):
            net.check_host("api.themoviedb.org", 443)


if __name__ == "__main__":
    unittest.main()
