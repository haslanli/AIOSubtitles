"""alass.run against stand-ins for the alass program (small shell scripts): its answer, its failures, and a run past
its time limit, which is a failure like the others (AlassFailed), not an error that ends the language's judging."""
import os
import stat
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import TempDir
from subsync import alass, programs
from subsync.settings import available_cpus

REF = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
CANDIDATE = "1\n00:00:05,000 --> 00:00:06,000\nhello\n"


class AlassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)

    def stand_in(self, script):
        path = os.path.join(self.dir, "alass")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(path, stat.S_IRWXU)
        return path

    def test_the_retimed_copy_is_returned(self):
        # alass is given the reference, the candidate and the output, all SRT files
        prog = self.stand_in('case "$1$2$3" in *.srt*.srt*.srt) cp "$2" "$3";; *) exit 9;; esac')
        self.assertEqual(alass.run(REF, CANDIDATE, alass=prog), CANDIDATE)

    def test_a_failing_alass(self):
        for script, why in (("exit 3", "alass exit 3"), ("exit 0", "alass exit 0")):   # the second writes nothing
            with self.subTest(script=script):
                with self.assertRaises(alass.AlassFailed) as e:
                    alass.run(REF, CANDIDATE, alass=self.stand_in(script))
                self.assertEqual(str(e.exception), why)

    def test_a_run_past_the_time_limit_is_a_failure(self):
        prog = self.stand_in("exec sleep 30")
        t0 = time.monotonic()
        with self.assertRaises(alass.AlassFailed) as e:
            alass.run(REF, CANDIDATE, alass=prog, timeout=1)
        self.assertEqual(str(e.exception), "alass timed out after 1 s")
        self.assertLess(time.monotonic() - t0, 10)

    def test_runs_wait_while_the_process_runs_its_most_at_once(self):
        # Every place's file fits its languages at once (SUBSYNC_PARALLEL_LANGUAGES), so up to places x 4
        # alass ran together, 0.4 to 1 GB each; at most one per processor run at once in the whole process now. Here
        # the bound is one: each run makes a folder only one may hold, and notes when it finds another's
        running, overlaps = os.path.join(self.dir, "running"), os.path.join(self.dir, "overlaps")
        prog = self.stand_in(f'mkdir "{running}" 2>/dev/null || echo overlap >> "{overlaps}"; sleep 0.3; '
                             f'rmdir "{running}" 2>/dev/null; cp "$2" "$3"')
        self.assertEqual(alass.RUNS_AT_ONCE, available_cpus())
        got = []
        with mock.patch.object(alass, "_runs", threading.BoundedSemaphore(1)):
            runs = [threading.Thread(target=lambda: got.append(alass.run(REF, CANDIDATE, alass=prog, timeout=10)))
                    for _ in range(3)]
            for t in runs:
                t.start()
            for t in runs:
                t.join(20)
        self.assertEqual(got, [CANDIDATE] * 3)
        self.assertFalse(os.path.exists(overlaps), "two alass runs at once")

    def test_on_linux_a_run_has_its_own_memory_and_processor_limits(self):
        # alass had only its wall-clock timeout, unlike ffmpeg's decoders; its memory grows with the time the
        # subtitles span, to GBs for a track or a subtitle made to span days. Here the shell sets the limits as it does
        # on Linux, and the stand-in reports them
        prog = self.stand_in('echo "$(ulimit -v) $(ulimit -t)" > "$3"')
        made = []

        def as_on_linux(cmd, *limits):
            made.append(programs.limited(cmd, *limits, linux=True))
            return made[-1]
        with mock.patch.object(alass, "limited", as_on_linux):
            memory, cpu = alass.run(REF, CANDIDATE, alass=prog).split()
        self.assertEqual(made[0][:2], ["sh", "-c"])
        self.assertEqual(made[0][2], f'ulimit -v {alass.MEMORY_KB} 2>/dev/null; ulimit -t {alass.CPU_S} 2>/dev/null; '
                                     'exec "$@"')
        self.assertEqual(made[0][3:5], ["alass", prog])                     # $0, then alass itself
        self.assertEqual([os.path.basename(f) for f in made[0][5:]], ["ref.srt", "cand.srt", "out.srt"])
        self.assertEqual(cpu, str(alass.CPU_S))
        if sys.platform.startswith("linux"):    # macOS's shell can't set an address-space limit
            self.assertEqual(memory, str(alass.MEMORY_KB))
        self.assertEqual((alass.MEMORY_KB, alass.CPU_S, alass.TIMEOUT_S), (3 * 1024 * 1024, 120, 120))
        cmd = ["alass", "r.srt", "c.srt", "o.srt"]
        self.assertEqual(programs.limited(cmd, "alass", 1, 2, linux=False), cmd)    # elsewhere: as it is

    @unittest.skipUnless(sys.platform.startswith("linux"), "the address-space limit is set on Linux only")
    def test_a_run_past_its_memory_limit_is_a_failure(self):
        # 4 GiB of address space asked (and never touched) past the 3 GiB limit: the run fails as alass's failures do
        prog = self.stand_in('python3 -c "import mmap; mmap.mmap(-1, 4 << 30)" && cp "$2" "$3"')
        with self.assertRaises(alass.AlassFailed) as e:
            alass.run(REF, CANDIDATE, alass=prog)
        self.assertEqual(str(e.exception), "alass exit 1")


if __name__ == "__main__":
    unittest.main()
