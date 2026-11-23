"""The tests' own stand-ins and set-up (helpers.py, imagesubs.py): what other tests rely on them for."""
import ast
import collections
import http.client
import http.server
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
import types
import unittest
import urllib.request
from unittest import mock

import helpers
import imagesubs as I
from helpers import (BACKLOG, FIXTURES, have, HAVE_CRYPTOGRAPHY, INSTANCE, local_lookup, Logs, NEEDS, ocr_models_in,
                     PASSWORD, RangeServer, REALDEBRID, RecheckHarness, ServerHarness, SignupHarness, SOURCE,
                     SourcesHarness, start_server, stop_server, TempDir, TORBOX, TORRENTIO, traced_memory, wait_until,
                     watch_the_log)
from subsync import net

TESTS = os.path.dirname(os.path.abspath(__file__))


class OnePathSetupTest(unittest.TestCase):
    """Helpers is the one place that puts the package on the import path. Every test module imports it before
    anything from subsync and changes sys.path in no other way, so all of them test the same copy and each one also
    runs on its own. What modules share is in helpers, and no module here imports a test module, so none runs
    another's top level or collects its TestCase classes."""

    @staticmethod
    def modules(prefix="test_"):
        for name in sorted(os.listdir(TESTS)):
            if name.startswith(prefix) and name.endswith(".py"):
                with open(os.path.join(TESTS, name), encoding="utf-8") as f:
                    yield name, ast.parse(f.read())

    @staticmethod
    def imported_test_modules(tree):
        """The test modules a module imports, wherever it does (in a function too, or through __import__)."""
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names += [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Call) and ast.unparse(node.func) in ("__import__", "importlib.import_module") \
                    and node.args and isinstance(node.args[0], ast.Constant):
                names.append(str(node.args[0].value))
        return [n for n in names if n.split(".")[-1].startswith("test_")]

    def test_every_test_module_imports_helpers_first_and_no_test_module(self):
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
        for name, tree in self.modules(prefix=""):          # helpers and imagesubs too
            with self.subTest(module=name, imports="test modules"):
                self.assertEqual(self.imported_test_modules(tree), [])

    def test_no_test_module_changes_the_import_path_itself(self):
        for name, tree in self.modules():
            with self.subTest(module=name):
                edits = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "sys.path"]
                self.assertEqual(edits, [])

    def test_a_modules_main_guard_comes_after_all_its_tests(self):
        # `python3 test_x.py` runs unittest.main where the guard stands; classes defined after it never ran
        for name, tree in self.modules():
            with self.subTest(module=name):
                guards = [i for i, node in enumerate(tree.body) if isinstance(node, ast.If)
                          and ast.unparse(node.test) in ("__name__ == '__main__'", '__name__ == "__main__"')]
                self.assertIn(guards, ([], [len(tree.body) - 1]))

    @staticmethod
    def names_bound(target):
        """The names an assignment's target binds (none for an attribute or an item)."""
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [n for t in target.elts for n in OnePathSetupTest.names_bound(t)]
        if isinstance(target, ast.Starred):
            return OnePathSetupTest.names_bound(target.value)
        return []

    def test_no_module_binds_a_top_level_name_twice(self):
        # test_public bound FILE twice, a stored file's key and further down a request's query string; its
        # first tests read the name as they ran, so they stored their file under the query string
        for name, tree in self.modules(prefix=""):
            bound = collections.Counter()
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound[node.name] += 1
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        bound.update(self.names_bound(target))
            with self.subTest(module=name):
                self.assertEqual([n for n, times in bound.items() if times > 1], [])

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


class StartServerTest(unittest.TestCase):
    """start_server serves a stand-in, stop_server returns at once, and a burst of connections all get in
    (socketserver's listen queue of 5 reset the rest)."""

    def test_a_burst_of_connections_waits_for_the_server_and_is_answered(self):
        class Answer(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server, held = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer), threading.Event()
        take = server.get_request
        server.get_request = lambda: (held.wait(10), take())[1]      # takes no connection until the burst is in
        start_server(server)
        burst = []
        try:
            for _ in range(BACKLOG // 2):
                burst.append(socket.create_connection(server.server_address, timeout=2))
        finally:
            held.set()
            answered = [exchange_on(sock) for sock in burst]
            started = time.monotonic()
            stop_server(server)
        self.assertEqual(answered, [b"HTTP/1.0 200"] * (BACKLOG // 2))
        self.assertLess(time.monotonic() - started, 0.3)           # not up to serve_forever's half second


def exchange_on(sock):
    """The status line of the answer to a GET sent on `sock`, which is then closed."""
    with sock:
        sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data.split(b"\r\n", 1)[0][:12]


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


# a stand-in for each tool a fixture script runs: it writes the file it would make (python3: those in $MADE; ffmpeg its
# last argument; mkvmerge the one after -o), and the one named in $FAILING then fails, as a step that stops half done
STAND_IN = """#!/bin/sh
tool=$(basename "$0")
case "$tool" in
  python3) cat > /dev/null; for f in $MADE; do : > "$f"; done ;;
  ffmpeg) for a; do out=$a; done; : > "$out" ;;
  mkvmerge) : > "$3" ;;
esac
[ "$tool" != "$FAILING" ]
"""


@unittest.skipUnless(have("zsh"), "the fixture scripts are zsh scripts")
class FixtureScriptsTest(unittest.TestCase):
    """A fixture script leaves none of its own working files beside the fixtures, whether a step fails or not
    (they were removed only after every step had worked)."""

    def run_script(self, name, made, failing):
        """A copy of fixtures/`name` run in a folder of its own with STAND_IN for each tool, `failing` failing: (its exit
        code, the files left there)."""
        tmp = TempDir()
        folder = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        tools = os.path.join(folder, "tools")
        os.mkdir(tools)
        for tool in ("python3", "ffmpeg", "mkvmerge"):
            with open(os.path.join(tools, tool), "w") as f:
                f.write(STAND_IN)
            os.chmod(os.path.join(tools, tool), 0o755)
        work = os.path.join(folder, "fixtures")
        os.mkdir(work)
        shutil.copy(os.path.join(FIXTURES, name), work)
        env = {"PATH": os.pathsep.join((tools, "/usr/bin", "/bin")), "MADE": " ".join(made), "FAILING": failing or ""}
        out = subprocess.run([shutil.which("zsh"), os.path.join(work, name)], env=env, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=60)
        return out.returncode, set(os.listdir(work)) - {name}

    def test_the_working_files_go_whether_a_step_fails_or_not(self):
        for name, made, working, steps in (
                ("make_fixture.sh", ("tiny.srt", "tiny-offset.srt", "tone.expr"), {"tone.expr", "tiny-av.mkv"},
                 ("ffmpeg", "mkvmerge")),
                ("make_image_fixture.sh", ("lines.sup",), {"lines.sup"}, ("mkvmerge",))):
            for failing in (None, *steps):
                with self.subTest(script=name, failing=failing):
                    code, left = self.run_script(name, made, failing)
                    self.assertEqual(code != 0, failing is not None)
                    self.assertEqual(left & working, set())
                    self.assertTrue(left)                       # the steps ran: they made files of their own


class ImageBuildTest(unittest.TestCase):
    """The image build tests the image's own copy of the package, the one that holds the OCR models the build
    adds, and fails without them. Its tests imported the source's copy (no models), so the model tests were skipped in
    every build. Run here as the Dockerfile runs them: from a folder that holds a copy of the package, with
    AIOSUBTITLES_IMAGE_BUILD set; and as before without it."""

    def setUp(self):
        tmp = TempDir()
        root = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        # laid out as the image is: its folder (WORKDIR /opt/aiosubtitles/subsync) holds the package, and the release's
        # VERSION is beside that folder (/opt/aiosubtitles/VERSION), where the package reads it (version.py)
        self.image = os.path.join(root, "subsync")
        # the package before the build adds the OCR models: any a machine set up for OcrModelTest keeps in its source
        # are left out
        shutil.copytree(os.path.join(SOURCE, "subsync"), os.path.join(self.image, "subsync"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.onnx"))
        shutil.copy(os.path.join(os.path.dirname(SOURCE), "VERSION"), root)

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
        # the copy has no models: a failure inside the image build. Outside it the source's copy is tested: skipped
        # when the source has none; run when it has them, on a machine set up for OcrModelTest (whether they read
        # is OcrModelTest's own result in the run)
        source_has_them = ocr_models_in(os.path.join(SOURCE, "subsync", "ocr"))
        for image_build, code, summary in ((True, 1, "FAILED (failures=2)"), (False, 0, "OK (skipped=2)")):
            with self.subTest(image_build=image_build):
                out = self.python(image_build, "-m", "unittest", "discover", "-s", TESTS, "-t", TESTS,
                                  "-p", "test_imagetext.py", "-k", "OcrModelTest")
                if not image_build and source_has_them:
                    self.assertIn("Ran 2 tests", out.stderr)
                    self.assertNotIn("skipped", out.stderr)
                    continue
                self.assertEqual(out.returncode, code, out.stderr)
                self.assertIn("Ran 2 tests", out.stderr)
                self.assertIn(summary, out.stderr)


class TracedMemoryTest(unittest.TestCase):
    """traced_memory measures the block's own memory whether or not tracing was on as it began, and stops
    tracing only when it started it. The memory tests started and stopped it themselves: under -X tracemalloc they
    measured everything traced since the run began, and turned tracing off for the rest of the run."""

    @staticmethod
    def block():
        """(held, peak) as traced_memory gives them for a block that takes and keeps 2 MB."""
        with traced_memory() as traced:
            kept = bytearray(2_000_000)
            held, peak = traced()
        del kept
        return held, peak

    def test_with_tracing_on_the_block_alone_is_measured_and_tracing_stays_on(self):
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            self.addCleanup(tracemalloc.stop)
        before = bytearray(5_000_000)                     # held from before the block
        held, peak = self.block()
        self.assertTrue(tracemalloc.is_tracing())
        self.assertGreaterEqual(held, 2_000_000)
        self.assertLess(peak, 3_000_000)                  # the block's 2 MB, not the 5 MB from before
        del before

    def test_with_tracing_off_it_is_on_for_the_block_only(self):
        if tracemalloc.is_tracing():
            self.skipTest("tracing is on for the whole run (-X tracemalloc)")
        held, peak = self.block()
        self.assertFalse(tracemalloc.is_tracing())
        self.assertGreaterEqual(held, 2_000_000)
        self.assertLess(peak, 3_000_000)


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


class LogsTest(unittest.TestCase):
    """The log lines a public harness reads are every line made meanwhile, a logged exception's
    traceback included, and the harness leaves the root logger as it found it (it was left at DEBUG for the rest of
    the run)."""

    def setUp(self):
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        root.setLevel(logging.WARNING)
        self.handlers = list(root.handlers)

    def left_as_found(self):
        root = logging.getLogger()
        self.assertEqual((root.level, root.handlers), (logging.WARNING, self.handlers))

    def test_every_line_with_its_traceback_then_logging_put_back(self):
        logs = Logs().on()
        try:
            logging.getLogger("test.logs").debug("a debug line")
            try:
                raise ValueError("held by the exception only")
            except ValueError:
                logging.getLogger("test.logs").warning("failed", exc_info=True)
        finally:
            logs.off()
        self.assertEqual(logs.lines[0], "a debug line")
        self.assertTrue(logs.lines[1].startswith("failed\nTraceback"), logs.lines[1])
        self.assertIn("ValueError: held by the exception only", logs.lines[1])
        self.left_as_found()

    def test_two_at_once_put_logging_back_whatever_order_they_go_off_in(self):
        # Each put back the level its own on had found, so of two harnesses closed out of order (a test's
        # second one closed by a cleanup, after its tearDown closed the first), the second set the root to DEBUG again
        for first_off in (0, 1):
            with self.subTest(first_off=first_off):
                both = [Logs().on(), Logs().on()]
                logging.getLogger("test.logs").debug("a line for both")
                both.pop(first_off).off()
                self.assertEqual(logging.getLogger().level, logging.DEBUG)      # one still on: still every line
                both[0].off()
                self.left_as_found()

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_each_public_harness_leaves_the_root_logger_as_it_found_it(self):
        # one test on each harness, a TestCase that takes it as its first base as its modules' tests do, each doing what
        # test_public_http's test_user_manifest, test_recheck's test_off_with_zero_hours and test_signup's
        # test_real_debrid_with_torbox_warns_and_leaves_it_out do (the harnesses are helpers', so no test
        # module is imported to run them)
        from helpers import http as fetch          # (`http` is the standard library's package here)
        from subsync import explain

        class Server(ServerHarness, unittest.TestCase):
            def setUp(case):
                case.start()

            def test(case):
                _, _, _, _, token = case.sign_up()
                code, body, _ = fetch(f"{case.base}/u/{token}/manifest.json")
                m = json.loads(body)
                case.assertEqual(code, 200)
                case.assertEqual((m["id"], m["name"], m["resources"]),
                                 ("org.aiosubtitles.public", "AIOSubtitles", ["subtitles"]))

        class Recheck(RecheckHarness, unittest.TestCase):
            def test(case):
                _, token = case.sign_up()
                before = len(case.fake.requests)
                case.assertFalse(case.checker(SUBSYNC_RECHECK_HOURS="0").maybe_start(token))
                case.assertEqual(len(case.fake.requests), before)

        class Signup(SignupHarness, unittest.TestCase):
            def test(case):
                uuid = case.fake.add({"services": [dict(TORBOX), dict(REALDEBRID)], "presets": [dict(TORRENTIO)]},
                                     PASSWORD)
                fields = {"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                          "languages": "English", "add_addon": "on"}
                made = case.signup.signup(case.s, case.users, fields, api_factory=case.api, timeout=10)
                case.assertEqual(made.warning, explain.PUBLIC_REAL_DEBRID_WARNING)

        for kind, harness in ((Server, ServerHarness), (Recheck, RecheckHarness), (Signup, SignupHarness)):
            with self.subTest(harness=harness.__name__):
                result = unittest.TestResult()
                kind("test").run(result)
                self.assertEqual((result.testsRun, result.errors, result.failures), (1, [], []))
                self.left_as_found()


class HalfwaySetUpTest(unittest.TestCase):
    """A harness whose set-up fails halfway closes each part it had made, as the installer's does.
    unittest runs no tearDown after a setUp that failed, and close() took every part for made (a failed start() in a
    test's body then failed again on the server it never got): the root logger was left at DEBUG with the harness's
    handler on, the stand-in AIOStreams serving, the data folder on disk, SourcesHarness's patch of
    socket.create_connection on for every later test."""

    def setUp(self):
        root = logging.getLogger()
        self.found = (root.level, list(root.handlers), socket.create_connection)

    def left_as_found(self):
        root = logging.getLogger()
        self.assertEqual((root.level, root.handlers, socket.create_connection), self.found)

    def run_failing(self, kind):
        """`kind`'s one test, run: its one error is the failure of a part of the set-up."""
        result = unittest.TestResult()
        case = kind("test")
        case.run(result)
        self.assertEqual((result.testsRun, len(result.errors), result.failures), (1, 1, []))
        self.assertIn("RuntimeError: a later part failed", result.errors[0][1])
        return case

    def assert_closed(self, *databases):
        for db in databases:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_a_public_harness_closes_what_it_had_made(self):
        class Server(ServerHarness, unittest.TestCase):
            def setUp(case):
                case.start()

            def test(case):
                pass

        class InBody(ServerHarness, unittest.TestCase):         # started in the test itself
            def test(case):
                case.start()

        class Recheck(RecheckHarness, unittest.TestCase):
            def test(case):
                pass

        class Signup(SignupHarness, unittest.TestCase):
            def test(case):
                pass

        # the service's pool fails after the store; a public harness's last part, its Logs, fails after the rest
        for kind, failing in ((Server, "subsync.public.Pool"), (InBody, "subsync.public.Pool"),
                              (Recheck, "helpers.Logs.on"), (Signup, "helpers.Logs.on")):
            with self.subTest(harness=kind.__name__):
                with mock.patch(failing, side_effect=RuntimeError("a later part failed")):
                    case = self.run_failing(kind)
                self.left_as_found()
                self.assertFalse(os.path.exists(case.dir))                       # the data folder
                self.assertEqual(case.fake.server.socket.fileno(), -1)           # the stand-in AIOStreams
                self.assert_closed(case.users.db, *([case.store.db] if kind in (Server, InBody) else []))

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_records_a_test_reopens_are_closed_too(self):
        # the scans for a secret on disk close the people's records and open them again (test_signup, test_recheck):
        # the harness closes the ones there when it ends, as it did before it held each part's undoing
        from subsync.users import Users
        opened = []

        class Reopens(SignupHarness, unittest.TestCase):
            def test(case):
                case.users.close()
                case.users = Users(os.path.join(case.dir, "users.sqlite3"), case.s.secret_bytes())
                opened.append(case.users)

        result = unittest.TestResult()
        Reopens("test").run(result)
        self.assertEqual((result.testsRun, result.errors, result.failures), (1, [], []))
        self.assert_closed(opened[0].db)

    def test_the_sources_harness_takes_its_patch_off(self):
        class Sources(SourcesHarness, unittest.TestCase):
            def test(case):
                pass
        with mock.patch("helpers.FakeSources", side_effect=RuntimeError("a later part failed")):
            self.run_failing(Sources)
        self.left_as_found()


class WaitUntilTest(unittest.TestCase):
    def test_a_step_of_the_wall_clock_neither_stretches_nor_cuts_the_wait(self):
        # Its time was the wall clock's: stepped back (NTP) it waited for ever, stepped forward it gave up at once
        for step in (-3.0, 3.0):
            looks, gave = [], []

            def stepped():                      # each look at the wall clock is `step` seconds past the last
                looks.append(1)
                return time.time() + step * len(looks)
            with self.subTest(step=step), \
                    mock.patch.object(helpers, "time", types.SimpleNamespace(**dict(vars(time), time=stepped))):
                t0 = time.monotonic()
                t = threading.Thread(target=lambda: gave.append(wait_until(lambda: False, 0.3)), daemon=True)
                t.start()
                t.join(3)
                self.assertFalse(t.is_alive())
                self.assertEqual(gave, [False])
                self.assertGreaterEqual(time.monotonic() - t0, 0.25)


class LocalLookupTest(unittest.TestCase):
    """For the whole run, only this machine's names are looked up, so a test that reaches for another machine
    fails before any lookup, whatever settings it leaves at their defaults, and every module keeps its "no network".
    No connection elsewhere is even tried here (the review's runner counts every one)."""

    def test_another_machine_is_never_looked_up(self):
        self.assertIs(socket.getaddrinfo, local_lookup)
        for host in ("opensubtitles-v3.strem.io", b"query.wikidata.org", "93.184.216.34", "2606:4700:4700::1111"):
            with self.subTest(host=host):
                with self.assertRaises(AssertionError):
                    socket.getaddrinfo(host, 443)
                self.assertEqual(helpers.refused_lookups(), [host.decode() if isinstance(host, bytes) else host])

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
        self.assertEqual(helpers.refused_lookups(), ["api.themoviedb.org"])

    def test_a_refusal_the_code_catches_still_fails_the_test(self):
        # The service catches the refusal where it leaves a failing source out (extra.py) or answers without
        # a language (origlang.py); a test that let a lookup out fails all the same, when it ends. No connection is
        # opened here: the names are refused at their lookup, before any (the review's runner counts every one)
        class Caught(unittest.TestCase):
            def test_a_refusal_caught_and_let_go(self):
                try:
                    socket.getaddrinfo("opensubtitles-v3.strem.io", 443)
                except Exception:                   # as extra.py's "a source that fails is left out"
                    pass

            def test_a_refusal_of_the_public_gate_caught(self):
                net.configure(public_only=True)     # its own resolver: the socket module's, as main_public
                self.addCleanup(net.configure, public_only=False)
                try:
                    net.check_host("api.themoviedb.org", 443)
                except Exception:
                    pass

            def test_nothing_looked_up(self):
                pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Caught).run(result)
        self.assertEqual((result.testsRun, result.errors), (3, []))
        failed = {test._testMethodName: trace for test, trace in result.failures}
        self.assertEqual(sorted(failed), ["test_a_refusal_caught_and_let_go", "test_a_refusal_of_the_public_gate_caught"])
        self.assertIn("a lookup of opensubtitles-v3.strem.io: tests reach nothing but this machine",
                      failed["test_a_refusal_caught_and_let_go"])
        self.assertIn("a lookup of api.themoviedb.org: tests reach nothing but this machine",
                      failed["test_a_refusal_of_the_public_gate_caught"])
        self.assertEqual(helpers.refused_lookups(), [])         # each name fails one test, the one it was refused in

if __name__ == "__main__":
    unittest.main()
