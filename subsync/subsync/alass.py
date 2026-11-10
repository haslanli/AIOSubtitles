"""alass sub-to-sub (alass-cli 2.0.0, GPL-3.0, run as a separate program): candidate re-timed to a reference.

Defaults: split penalty, frame-rate guessing on. Its result is never trusted on
its own (it can produce jumps of tens of seconds); the pipeline gates it.
"""
import os
import subprocess
import tempfile


class AlassFailed(Exception):
    pass


def run(reference_srt, candidate_text, candidate_ext, alass="alass", timeout=120):
    """reference_srt: SRT text; candidate_text: the subtitle (srt or ass). Returns the re-timed text."""
    ext = "ass" if candidate_ext in ("ass", "ssa") else "srt"
    with tempfile.TemporaryDirectory(prefix="subsync-") as d:
        ref, cand, out = os.path.join(d, "ref.srt"), os.path.join(d, f"cand.{ext}"), os.path.join(d, f"out.{ext}")
        with open(ref, "w", encoding="utf-8") as f:
            f.write(reference_srt)
        with open(cand, "w", encoding="utf-8") as f:
            f.write(candidate_text)
        p = subprocess.run([alass, ref, cand, out], capture_output=True, timeout=timeout)
        if p.returncode != 0 or not os.path.exists(out):
            raise AlassFailed(f"alass exit {p.returncode}")
        with open(out, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
