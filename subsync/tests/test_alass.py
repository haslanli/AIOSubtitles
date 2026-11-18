"""alass.run against stand-ins for the alass program (small shell scripts): its answer, its failures, and a run past
its time limit, which is a failure like the others (AlassFailed), not an error that ends the language's judging."""
import os
import stat
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import TempDir  # noqa: E402
from subsync import alass  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
