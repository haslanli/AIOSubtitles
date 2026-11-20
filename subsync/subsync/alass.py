"""alass sub-to-sub (alass-cli 2.0.0, GPL-3.0, run as a separate program): candidate re-timed to a reference.

Defaults: split penalty, frame-rate guessing on. Its result is never trusted on
its own (it can produce jumps of tens of seconds); the pipeline gates it.
"""
import os
import subprocess
import tempfile
import threading

from .settings import available_cpus

# alass runs at once in the whole process, every person's worker and every language of a job together: one per
# processor. The places (SUBSYNC_SYNCS_AT_ONCE, auto: one per processor) bound the files worked on, but each file's
# languages are fitted at once (SUBSYNC_PARALLEL_LANGUAGES, up to 4), and one run on a film's subtitles keeps a
# processor busy for seconds and takes 0.4 to 1 GB
RUNS_AT_ONCE = available_cpus()
_runs = threading.BoundedSemaphore(RUNS_AT_ONCE)


class AlassFailed(Exception):
    pass


def run(reference_srt, candidate_srt, alass="alass", timeout=120):
    """Both texts are timing-only SRT (the pipeline's timing_srt and plain_srt). Returns the candidate re-timed, as SRT
    text. Raises AlassFailed when alass fails or runs longer than `timeout` seconds (it is stopped then). A run waits
    while RUNS_AT_ONCE others run; its `timeout` counts from its start."""
    with tempfile.TemporaryDirectory(prefix="subsync-") as d:
        ref, cand, out = os.path.join(d, "ref.srt"), os.path.join(d, "cand.srt"), os.path.join(d, "out.srt")
        with open(ref, "w", encoding="utf-8") as f:
            f.write(reference_srt)
        with open(cand, "w", encoding="utf-8") as f:
            f.write(candidate_srt)
        try:
            with _runs:
                p = subprocess.run([alass, ref, cand, out], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AlassFailed(f"alass timed out after {timeout} s") from None
        if p.returncode != 0 or not os.path.exists(out):
            raise AlassFailed(f"alass exit {p.returncode}")
        with open(out, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
