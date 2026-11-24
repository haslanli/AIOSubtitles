"""alass sub-to-sub (alass-cli 2.0.0, GPL-3.0, run as a separate program): candidate re-timed to a reference.

Defaults: split penalty, frame-rate guessing on. Its result is never trusted on
its own (it can produce jumps of tens of seconds); the pipeline gates it.
"""
import os
import subprocess
import tempfile
import threading

from .programs import limited
from .settings import available_cpus

# alass runs at once in the whole process, every person's worker and every language of a job together: one per
# processor. The places (SUBSYNC_SYNCS_AT_ONCE, auto: one per processor) bound the files worked on, but each file's
# languages are fitted at once (SUBSYNC_PARALLEL_LANGUAGES, up to 4), and one run on a film's subtitles keeps a
# processor busy for seconds and takes 0.4 to 1 GB
RUNS_AT_ONCE = available_cpus()
_runs = threading.BoundedSemaphore(RUNS_AT_ONCE)
# One run's limits (programs.limited). alass's memory grows with the time the subtitles span, whatever their
# lines: a real 3-hour pair takes 1.1 to 1.2 GB, while a track or a subtitle made to span days (cue times are the
# file's, and nothing else bounds them) took 4 GB and more; past the limit the run fails and the original is kept
TIMEOUT_S = 120                     # the longest a run may take, then it is stopped
MEMORY_KB = 3 * 1024 * 1024         # address space of a run on Linux (3 GiB)
CPU_S = TIMEOUT_S                   # processor seconds of a run on Linux (alass runs on one thread)
# alass prints its errors, with its progress, on its normal output: "error: <what failed>", then "caused by: <why>"
# lines, the last the closest to the cause. That last line is kept with a failure, at most ERROR_CHARS long;
# it names only the run's own files and their placeholder text (timing_srt, plain_srt), no subtitle's words
ERROR_LABELS = ("caused by:", "error:")
ERROR_CHARS = 200


class AlassFailed(Exception):
    pass


def _last_error(output):
    """alass's last error line in its output (bytes), without its label, cut to ERROR_CHARS; "" when it printed none."""
    for line in reversed(output.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        for label in ERROR_LABELS:
            if line.startswith(label):
                return line[len(label):].strip()[:ERROR_CHARS]
    return ""


def run(reference_srt, candidate_srt, alass="alass", timeout=TIMEOUT_S):
    """Both texts are timing-only SRT (the pipeline's timing_srt and plain_srt). Returns the candidate re-timed, as SRT
    text. Raises AlassFailed when alass fails ("alass exit 1: <its last error line>"), writes no output, passes its
    limits on Linux (MEMORY_KB, CPU_S) or runs longer than `timeout` seconds (it is stopped then). alass gets no input.
    A run waits while RUNS_AT_ONCE others run; its `timeout` counts from its start."""
    with tempfile.TemporaryDirectory(prefix="subsync-") as d:
        ref, cand, out = os.path.join(d, "ref.srt"), os.path.join(d, "cand.srt"), os.path.join(d, "out.srt")
        with open(ref, "w", encoding="utf-8") as f:
            f.write(reference_srt)
        with open(cand, "w", encoding="utf-8") as f:
            f.write(candidate_srt)
        try:
            with _runs:
                p = subprocess.run(limited([alass, ref, cand, out], "alass", MEMORY_KB, CPU_S), capture_output=True,
                                   stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AlassFailed(f"alass timed out after {timeout} s") from None
        if p.returncode != 0:
            said = _last_error(p.stdout)
            raise AlassFailed(f"alass exit {p.returncode}: {said}" if said else f"alass exit {p.returncode}")
        if not os.path.exists(out):
            raise AlassFailed("alass wrote no output")
        with open(out, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
