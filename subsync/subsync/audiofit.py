"""Audio route: a windowed fit on a few short audio windows.

K fit windows of W s, frame-rate scales (a scale must beat 1.0 clearly), per-window offsets confirmed by a second
window, bisection of each offset change, the split in subtitle order chosen from speech onsets, lines of a removed
scene dropped, per-segment refinement to speech onsets, then a gate on V held-out windows never used for fitting.
Shifts under 100 ms on one segment are never published.

A copy the speech gate refuses still passes when it is clearly better (`better`): a subtitle made by someone else
never starts its lines exactly where the speech starts, so after a perfect shift only about 60 % of its lines are
within 200 ms of the detected speech (Primer's YTS MP4), while the original may be seconds off. It passes when the
held-out windows match at least `matched` lines with at most `miss` unmatched, a median error of at most `median_ms` and
at least `within` of the matched lines within 200 ms, and the lines moved by at least `min_shift_s` (median). The
held-out windows, never used for the fit, are the confirmation: on a real film most fit windows give no label of their
own (music, silence) and a mostly silent one may give a wrong one. An original the fit would move by at most
100 ms (one segment, no speed change) whose own lines meet the same numbers on all read windows fits the speech as it
is (subtitles made for Primer's YTS release, 52-58 ms median, 81 % within 200 ms) and is listed unchanged.

A file's windows fit its audio budget (`budget` bytes, shared by all subtitles of the file): from the file's
average byte rate, the windows get shorter than W when the planned windows would not fit; when they would have to be
shorter than MIN_W, the original is kept without reading any audio, and a window that would pass the budget is not read.

On real releases subtitle lines start a steady ~190 ms before Silero's detected speech, so the bar is
applied after removing that lead: onsets are moved `lead` seconds earlier before fitting and gating (the release's
own offset when known, else a measured default; 0 on the synthetic corpus, whose speech onsets are exact).

D3, D5. Speech-run ends: a subtitle's line ends trail the speech by its author's own habit (reading time,
minimum display), unknown in advance, so an end can't move a whole fit; but where the fit changes shift, the line after
which it changes is chosen by onsets and ends alike, each end measured against the subtitle's own lag (the median of its
ends against speech-run ends under the fit; END_WEIGHT an end). The held-out check counts onsets only. Close
calls: when the first verdict keeps the original with held-out numbers within CLOSE of the pass numbers, or
passes on fewer than CLOSE_MATCHED matched lines, or a shift change rests on fewer than 2 fit windows, up to `extra`
more windows (two thirds held-out, a third fit windows, in the largest gaps between the windows read) are read, 4 at a
time, as many as the audio budget leaves, and the file is judged again on all of them; that verdict is the result.
"""
import bisect
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .langs import lang3, same_language
from .mkv import NotMatroska
from .mp4 import NoAudioThere
from .reader import ReadFailed, Refused, Stopped
from .relgate import within
from .vad import SR, SileroVad

DT = 0.05           # the correlation's frame (s): a window's 10 ms speech frames are taken every fifth
SCALES = [1.0, 25 / (24000 / 1001), (24000 / 1001) / 25, 24 / (24000 / 1001), (24000 / 1001) / 24, 25 / 24, 24 / 25]
SCALE_GAIN = 0.1    # a scale other than 1.0 must score this much more per fit window than 1.0
OMAX = 600.0        # the largest offset tried, each way (s)
PAD = int(OMAX / DT)
LAGS = np.arange(-PAD, PAD + 1) * DT   # the offsets a window is scored at (s)
MIN_W = 10.0        # shorter windows hold too few lines for the held-out check
PLANNED = 4         # windows a fit reads beyond its K + V (a confirmation, bisections, extra held-out windows)
MIN_WINDOW_LINES = 5  # a window whose usual place holds fewer of the subtitle's lines is moved to where they are
PLACE_STEP = 2.0    # the starts place() looks at in a stretch, this far apart (s)
COST = 1.25         # a window's read costs about 1.25 x its length at the average byte rate (Cues, interleaving)
PARALLEL_READS = 4  # planned windows read at once (AudioFit.prefetch)
FULL_WINDOW = 0.95  # a window decoded to less of its length is a failed read, never judged
RUN_GAP = 6         # speech runs (10 ms frames): gaps under 60 ms are closed,
RUN_MIN = 10        # and runs under 100 ms dropped
SAME_OFFSET_S = 0.5   # two windows' offsets this close are the same offset
FITS_Z = 0.3        # a window fits an offset where it scores at least this (per 50 ms frame, -1 to 1)
OWN_PEAK_Z = 0.45   # a window's own offset when its best score is at least this
OWN_GAIN_Z = 0.15   # and this much above its score at the global offset
CUT_ERR_S = 0.4     # where a shift change is placed, an onset (or end) this close to the speech counts
END_WEIGHT = 0.5    # a line end that lands counts half an onset where a shift change is placed
# the speech gate on held-out lines: at least GATE_LINES matched, a median error of at most GATE_MEDIAN_MS, at
# least GATE_WITHIN of them within WITHIN_MS, at most GATE_MISS unmatched, and of all lines at least GATE_STRICT within
# WITHIN_MS (GATE_STRICT_ORIGINAL for an original judged as it is)
GATE_LINES = 25
GATE_MEDIAN_MS = 100
WITHIN_MS = 200
GATE_WITHIN = 0.90
GATE_MISS = 0.30
GATE_STRICT = 0.80
GATE_STRICT_ORIGINAL = 0.90
# the second way to pass, the defaults of settings' SUBSYNC_AUDIO_BETTER_*; with that rule off, close calls are
# still measured against it
BETTER = {"matched": 25, "miss": 0.30, "median_ms": 150.0, "within": 0.55, "min_shift_s": 1.0}
CLOSE = 0.2         # a verdict within 20 % of the pass numbers is a close call
CLOSE_MATCHED = 40  # a pass on fewer matched lines is a close call


def planned_window(budget, rate, k=8, v=4, w=30.0):
    """The length of a file's audio windows: W, or shorter so all K + V + PLANNED windows fit the audio budget at the
    file's byte rate; None when they would be shorter than MIN_W (the file keeps its original, no audio is read)."""
    if not (budget and rate):
        return w
    fit = budget / ((k + v + PLANNED) * COST * rate)
    return None if fit < MIN_W else min(w, round(fit, 1))


def most_read(rate, budget=None, extra=0, tracks=1, k=8, v=4, w=30.0):
    """The most an audio sync of a file could read, in bytes: every planned window and every close-call
    window, on each audio track it may fit (fit_tracks: two when a second language's track exists), within the budget.
    The whole budget when the file's byte rate isn't known; 0 when its windows would be too short to read."""
    if not rate:
        return budget or 0
    length = planned_window(budget, rate, k, v, w)
    if length is None:
        return 0
    total = int((k + v + PLANNED + extra) * COST * length * rate * max(1, tracks))
    return min(total, int(budget)) if budget else total


def peak(vals, near):
    """Where a score curve over LAGS is highest; of equal highest scores the offset nearest `near`, so which of tied
    offsets wins never depends on rounding."""
    top = np.flatnonzero(vals >= vals.max() - 1e-9)
    return int(top[np.argmin(np.abs(LAGS[top] - near))])


def _audio_tracks(index):
    """The file's audio tracks (MkvIndex.audio_tracks); [] when the index lists none (an MP4: its one track)."""
    return (index.audio_tracks() or []) if hasattr(index, "audio_tracks") else []


def second_track(index):
    """The audio track fit_tracks tries when the default one keeps the original: one in another language, or None."""
    tracks = _audio_tracks(index)
    default = next((t for t in tracks if t.get("default")), tracks[0] if tracks else None)
    return next((t for t in tracks if default is not None and t is not default
                 and lang3(t.get("language")) != lang3(default.get("language"))), None)


def _untagged(track):
    return not track.get("tagged", True) or lang3(track.get("language")) == "und"


def track_order(index, original=None):
    """The audio tracks fit_tracks tries, in order, each once: with the title's original language known, first the
    track in it (a track named as a commentary skipped; an untagged one, English by Matroska's rule, only for an English
    original and only when no track is tagged with it), then the default track, then second_track's. Without an
    original language, or with no track in it: the default, then second_track's, as before. [] when the file offers
    no list of audio tracks (MP4: its one track)."""
    tracks = _audio_tracks(index)
    if not tracks:
        return []
    default = next((t for t in tracks if t.get("default")), tracks[0])
    order = []
    if original:
        talk = [t for t in tracks if "commentary" not in (t.get("name") or "").lower()]
        first = next((t for t in talk if not _untagged(t) and same_language(t.get("language"), original)), None)
        if first is None and same_language(original, "eng"):
            first = next((t for t in talk if _untagged(t)), None)
        if first is not None:
            order.append(first)
    for t in (default, second_track(index)):
        if t is not None and all(o["index"] != t["index"] for o in order):
            order.append(t)
    return order


def most_tracks(index, original=None):
    """How many audio tracks fit_tracks may try on a file (the room check's estimate): with the original language
    known, its order's length; unknown yet, the most any original language among the file's tracks would make (its
    answer may come before the audio sync does)."""
    if original:
        return max(1, len(track_order(index, original)))
    candidates = {lang3(t.get("language")) for t in _audio_tracks(index)} | {None}
    return max([1] + [len(track_order(index, c)) for c in candidates])


class AudioBudget(Exception):
    """The next audio window would pass the file's audio budget."""


class AudioFit:
    def __init__(self, index, lines, vad=None, k=8, w=30.0, v=4, lead=0.0, ffmpeg="ffmpeg", better=None, budget=None,
                 track=None, extra=0):
        self.index, self.allines = index, lines
        self.track = track          # an audio track index (ffmpeg 0:a:N); None = the file's default track
        self.track_arg = {} if track is None else {"track": track}   # window_pcm's (an MP4 has one track, no argument)
        self.better = better
        self.budget = budget
        self.extra = extra          # windows a close call may add (D5)
        self.lines = [l for l in lines if l["kind"] == "dialogue"]
        self.vad = vad or SileroVad()
        self.K, self.W, self.V, self.lead, self.ffmpeg = k, w, v, lead, ffmpeg
        # decoded windows are kept on the file's index, so the next subtitle (and the other language) of the same file
        # reuses them: the first K windows sit at fixed places, and one fit read 4 GB of a 5.5 GB file
        try:
            self.cache = index.__dict__.setdefault("audio_windows", {})
            self.file_lock = index.__dict__.setdefault("audio_prefetch_lock", threading.Lock())
        except AttributeError:
            self.cache, self.file_lock = {}, threading.Lock()
        self.windows_read = 0
        self.audio_s = 0.0
        self._lock = threading.Lock()

    # -- audio -------------------------------------------------------------------------------------------------
    def rate(self):
        """The file's average bytes per second (0 when unknown)."""
        d = self.index.duration()
        return (getattr(self.index, "size", 0) or 0) / d if d else 0.0

    def _key(self, start, length):
        return (round(start, 2), round(length, 2), round(self.lead, 3), self.track)

    def read_window(self, start, length):
        key = self._key(start, length)
        if key not in self.cache:
            if self.budget and getattr(self.index, "audio_bytes", 0) + COST * length * self.rate() > self.budget:
                raise AudioBudget()
            self.cache[key] = self._read_window(start, length)
        return self.cache[key]

    def prefetch(self, starts, length):
        """The planned windows not read yet, up to PARALLEL_READS at once (12 Angry Men: 14 windows one after another
        took 27 s from the pick to the first verdict, past the pick's 14 s wait). Only as many as the audio budget
        allows; a window whose read fails is left for read_window, which reads it again in the fit's order and raises
        there. The same windows and bytes as reading them one by one. The windows read are kept on the file's index,
        and that is what the next subtitle or language of the file reuses. The pipeline runs one audio fit at a time
        (the worker's audio_lock); the file's lock is only a guard for a caller that runs two fits of one file at once,
        which then don't read a window twice here (read_window itself takes no lock)."""
        with self.file_lock:
            self._prefetch(starts, length)

    def _prefetch(self, starts, length):
        want, spent = [], getattr(self.index, "audio_bytes", 0)
        for start in starts:
            key = self._key(start, length)
            if key in self.cache or any(k == key for k, _ in want):
                continue
            spent += COST * length * self.rate()
            if self.budget and spent > self.budget:
                break
            want.append((key, start))
        if len(want) < 2:
            return
        with ThreadPoolExecutor(max_workers=PARALLEL_READS, thread_name_prefix="subsync-audio") as pool:
            futures = [(key, pool.submit(self._read_window, start, length)) for key, start in want]
            for key, fut in futures:
                try:
                    self.cache[key] = fut.result()
                except (Refused, Stopped):   # the host refused, or the guard stopped the job: no further read
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception:   # left for the ordered read (read_window) to read again and raise
                    pass

    def _read_window(self, start, length):
        """A window's speech: its 10 ms frames' mask, the onsets (moved `lead` earlier) and ends of its speech runs, and
        `len`, the seconds decoded. Past the end of the file's audio (NoAudioThere) a window without speech; a window
        decoded short (a read or decoder that stopped early) raises ReadFailed and is never kept."""
        try:
            pcm = self.index.window_pcm(start, length, SR, self.ffmpeg, **self.track_arg)
        except NoAudioThere:
            pcm = None
        with self._lock:
            self.windows_read += 1
            self.audio_s += length
        fl = SR // 100  # 10 ms frames
        if pcm is None:
            frames, speech, decoded = int(length * SR) // fl, [], length
        else:
            want = int(max(0.0, min(length, self.index.duration() - start)) * SR) * 2   # none past the file's end
            if len(pcm) < FULL_WINDOW * want:
                raise ReadFailed(f"short audio window ({len(pcm)} of {want} bytes)")
            audio = np.frombuffer(pcm, dtype=np.int16, count=len(pcm) // 2).astype(np.float32) / 32768
            frames, speech, decoded = len(audio) // fl, self.vad.speech(audio), min(length, len(audio) / SR)
        mask = np.zeros(frames, dtype=bool)
        for a, b in speech:
            mask[a // fl: b // fl] = True
        runs, i = [], 0
        while i < len(mask):
            if mask[i]:
                j = i
                while j < len(mask) and mask[j]:
                    j += 1
                runs.append([i, j])
                i = j
            else:
                i += 1
        merged = []
        for a, b in runs:  # close gaps < 60 ms, drop runs < 100 ms
            if merged and a - merged[-1][1] < RUN_GAP:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        merged = [r for r in merged if r[1] - r[0] >= RUN_MIN]
        mask[:] = False
        for a, b in merged:
            mask[a:b] = True
        onsets = np.array([start + a / 100 - self.lead for a, _ in merged])
        offsets = np.array([start + b / 100 for _, b in merged])
        return {"start": start, "len": decoded, "mask": mask, "onsets": onsets, "offsets": offsets}

    # -- correlation -------------------------------------------------------------------------------------------
    @staticmethod
    def sub_signal(lines, scale, n):
        s = -np.ones(n)
        for l in lines:
            a, b = int(l["s"] * scale / DT), int(l["e"] * scale / DT)
            s[max(a, 0):max(min(b, n), 0)] = 1
        return s

    @staticmethod
    def frames(w):
        """A window's 50 ms frames (at least 1, so a score per frame never divides by 0)."""
        return max(1, len(w["mask"][::5]))

    @staticmethod
    def fast_corr(w, ssig):
        """A window's score at every offset in LAGS: per 50 ms frame +1 where its speech and the subtitle's lines (moved
        by the offset) agree, -1 where they don't, with no line outside the signal. Only the stretch of the signal the
        window can reach is correlated (an FFT of the whole film's signal per window was most of a fit's CPU)."""
        v = np.where(w["mask"][::5], 1.0, -1.0)
        if not len(v):
            return LAGS, np.zeros(len(LAGS))
        lo = int(round(w["start"] / DT)) - PAD    # the signal's frame under the window's first at offset +OMAX
        seg = np.full(2 * PAD + len(v), -1.0)
        a, b = max(lo, 0), min(lo + len(seg), len(ssig))
        if a < b:
            seg[a - lo:b - lo] = ssig[a:b]
        return LAGS, np.correlate(seg, v, "valid")[::-1]

    @staticmethod
    def zat(w, curve, o):
        offs, vals = curve
        return float(vals[int(np.argmin(np.abs(offs - o)))]) / AudioFit.frames(w)

    @staticmethod
    def onset_errors(starts, windows, key="onsets"):
        """Signed (onset - start) per mapped start inside a read window, matched one-to-one; None = no onset. With
        key="offsets", speech-run ends against line ends."""
        errs = []
        for w in windows:
            inside = [s for s in starts if w["start"] + 0.5 <= s <= w["start"] + w["len"] - 0.5]
            marks = w.get(key, ())
            pairs = sorted((abs(o - s), i, j) for i, s in enumerate(inside) for j, o in enumerate(marks)
                           if abs(o - s) <= 1.0)
            used_l, used_o, got = set(), set(), {}
            for _, i, j in pairs:
                if i in used_l or j in used_o:
                    continue
                used_l.add(i)
                used_o.add(j)
                got[i] = float(marks[j] - inside[i])
            errs += [got.get(i) for i in range(len(inside))]
        return errs

    @staticmethod
    def gate(errs, need=GATE_LINES, strict_mode=False):
        matched = [abs(e) * 1000 for e in errs if e is not None]
        total = len(errs)
        if len(matched) < need:
            return {"n": total, "matched": len(matched), "pass": False, "why": "too few sampled lines"}
        med = float(np.median(matched))
        close = sum(within(x, WITHIN_MS) for x in matched)
        share = close / len(matched)
        miss = 1 - len(matched) / total
        strict = close / total
        return {"n": total, "matched": len(matched), "miss": round(miss, 3), "median_ms": round(med, 1),
                "within200": round(share, 3), "within200_misses_as_fail": round(strict, 3),
                "pass": within(med, GATE_MEDIAN_MS) and share >= GATE_WITHIN and miss <= GATE_MISS
                and strict >= (GATE_STRICT_ORIGINAL if strict_mode else GATE_STRICT)}

    def meets_better(self, g):
        """The numbers of the second way to pass: matched lines, unmatched share, median error, within 200 ms."""
        b = self.better
        if not b or g.get("median_ms") is None:
            return False
        return (g["matched"] >= b["matched"] and g["miss"] <= b["miss"] and g["median_ms"] <= b["median_ms"]
                and g["within200"] >= b["within"])

    def end_lag(self, c, o, windows):
        """The subtitle's own lag of line ends behind speech-run ends (median, s), from the lines whose start lands on a
        speech onset within 200 ms under the fit (so a part of the film with another shift doesn't count); None on fewer
        than 10."""
        errs = []
        for w in windows:
            on, off = np.asarray(w.get("onsets", ())), np.asarray(w.get("offsets", ()))
            if not len(on) or not len(off):
                continue
            for l in self.lines:
                s, e = l["s"] * c + o, l["e"] * c + o
                if s < w["start"] + 0.5 or e > w["start"] + w["len"] - 0.5 or np.min(np.abs(on - s)) > 0.2:
                    continue
                d = off - e
                k = int(np.argmin(np.abs(d)))
                if abs(d[k]) <= 1.0:
                    errs.append(float(d[k]))
        return float(np.median(errs)) if len(errs) >= 10 else None

    def clearly_better(self, g_new, moved):
        return self.meets_better(g_new) and moved >= self.better["min_shift_s"]

    # -- the fit -----------------------------------------------------------------------------------------------
    def run(self):
        """Returns {decision: in_sync|synced|keep_original, lines (new, for synced), model, gates, windows}."""
        if not self.index.duration() or not self.lines:
            return {"decision": "keep_original", "why": "no duration or no dialogue"}
        if self.budget and self.rate():
            w = planned_window(self.budget, self.rate(), self.K, self.V, self.W)
            if w is None:
                return {"decision": "keep_original", "windows": 0, "audio_s": 0.0,
                        "why": f"the file's bitrate is too high for the audio budget ({self.budget / 1e6:.0f} MB)"}
            self.W = w
        try:
            first = self._run()
        except AudioBudget:
            return {"decision": "keep_original", "windows": self.windows_read, "audio_s": self.audio_s,
                    "why": f"audio budget reached ({self.budget / 1e6:.0f} MB)"}
        except NotMatroska as e:    # no audio this module can decode (a window past the audio's end is NoAudioThere)
            return {"decision": "keep_original", "windows": self.windows_read, "audio_s": self.audio_s,
                    "why": f"the file's audio can't be read ({e})"}
        return self.close_call_again(first)

    def close_call(self, r):
        """Why a first verdict is a close call (D5), or None."""
        if not r.get("plan"):
            return None
        b = self.better or BETTER

        def near(g):
            return bool(g) and g.get("median_ms") is not None and g["matched"] >= (1 - CLOSE) * b["matched"] \
                and g["miss"] <= (1 + CLOSE) * b["miss"] and g["median_ms"] <= (1 + CLOSE) * b["median_ms"] \
                and g["within200"] >= (1 - CLOSE) * b["within"]
        if r["decision"] == "keep_original":
            return "close to the bar" if near(r.get("gate_new")) or near(r.get("gate_old")) else None
        g = r.get("gate_new") if r["decision"] == "synced" else r.get("gate_old")
        if (g or {}).get("matched", 0) < CLOSE_MATCHED:
            return "passed on few lines"
        offsets = r.get("segment_offsets") or []
        if len(offsets) > 1 and any(sum(abs(lab - o) <= SAME_OFFSET_S for _, lab in r.get("seq", [])) < 2
                                    for o in offsets):
            return "a shift change rests on few windows"
        return None

    def gaps(self, taken, n, lo, hi):
        """Up to n window starts in the middle of the largest gaps between windows already taken, in [lo, hi - W]."""
        W, out = self.W, []
        taken = sorted(taken)
        for _ in range(n):
            edges = [lo - W] + sorted(taken + out) + [hi]
            best = max(zip(edges, edges[1:]), key=lambda ab: ab[1] - ab[0])
            if best[1] - best[0] < 3 * W:
                break
            out.append(round((best[0] + W + best[1]) / 2 - W / 2, 2))
        return out

    def close_call_again(self, first):
        """A close call judged again with up to `extra` more windows, as many as the audio budget leaves."""
        why = self.close_call(first) if self.extra else None
        if not why:
            return first
        plan, D = first["plan"], self.index.duration()
        margin = 0.03 * D
        n_check = round(self.extra * 2 / 3)
        n_fit = self.extra - n_check
        read = [k[0] for k in self.cache if k[1:] == self._key(0.0, self.W)[1:]]   # fit, held-out and the rest
        fit_more = self.gaps(read, n_fit, margin, D - margin)       # never over a held-out window
        check_more = self.gaps(read + fit_more, n_check, margin, D - margin)
        more = check_more + fit_more
        if self.budget and self.rate():
            left = self.budget - getattr(self.index, "audio_bytes", 0)
            more = more[:max(0, int(left // (COST * self.W * self.rate())))]
        if not more:
            return dict(first, close_call={"why": why, "windows": 0})
        before = self.windows_read
        try:
            second = self._run(sorted(plan["fit"] + [x for x in fit_more if x in more]),
                               sorted(plan["check"] + [x for x in check_more if x in more]))
        except AudioBudget:
            return dict(first, close_call={"why": why, "windows": self.windows_read - before, "stopped": "audio budget"})
        second["close_call"] = {"why": why, "windows": self.windows_read - before,
                                "first": {"decision": first["decision"], "why": first.get("why"),
                                          "gate_new": first.get("gate_new")}}
        second["windows"] = self.windows_read
        second["audio_s"] = self.audio_s
        return second

    def place(self, usual, lo, hi, avoid=()):
        """A window's start: its usual place when the subtitle has at least MIN_WINDOW_LINES lines there; else, of the
        starts lo, lo + PLACE_STEP, ... whose window ends by hi, the first whose window holds the most of them. Never
        overlapping a window starting in `avoid`. (Moving every window to its stretch's busiest spot does worse on
        talkative films: rapid exchanges pair less surely with speech onsets.)"""
        W = self.W
        starts = sorted(l["s"] for l in self.lines)

        def lines_in(t):
            return bisect.bisect_left(starts, t + W) - bisect.bisect_left(starts, t)

        def free(t):
            return all(abs(t - a) >= W for a in avoid)
        if free(usual) and lines_in(usual) >= MIN_WINDOW_LINES:
            return usual
        best, best_n = usual, lines_in(usual) if free(usual) else -1
        room = (hi + 1e-6 - W - lo) / PLACE_STEP
        if not room >= 0:   # no start fits (or no runtime to place in)
            return best
        # from one step to the next, a window holds more lines only once it takes in another line's start, and becomes
        # free only once it clears an avoided window; so the first step with the most lines is the first step or one
        # of the two steps around such a place (a mark), and only those are looked at: the work follows the lines, not
        # the runtime (stepping through a stretch of an absurd duration, 1e17 s, never ended)
        marks = [(s - W - lo) / PLACE_STEP for s in starts] + [(a + W - lo) / PLACE_STEP for a in avoid]
        steps = {0} | {i for x in marks for i in (math.floor(x), math.floor(x) + 1) if 0 <= i <= room}
        for i in sorted(steps):
            t = lo + PLACE_STEP * i
            if free(t) and lines_in(t) > best_n:
                best, best_n = t, lines_in(t)
        return best

    def plan(self, D, margin, span):
        """Where the fit and the held-out check windows go: one in each of K (and V) equal stretches of the
        runtime, moved within its stretch to where the subtitle has lines when its usual place has almost none (a quiet
        film: Upstream Color heard 10 lines in evenly spread windows); check windows never overlap fit windows. Planned once
        per file and kept on its index, so every subtitle of the file reads the same windows."""
        try:
            plans = self.index.__dict__.setdefault("audio_plans", {})
        except AttributeError:
            plans = {}
        key = (self.K, self.V, round(self.W, 1))
        if key not in plans:
            W = self.W
            fit = [self.place(max(0.0, margin + (i + 0.5) * span - W / 2), margin + i * span, margin + (i + 1) * span)
                   for i in range(self.K)]
            vspan = (D - 2 * margin) / self.V
            check = [self.place(max(0.0, margin + (i + 0.5) * vspan + span / 4 - W / 2), margin + i * vspan,
                                margin + (i + 1) * vspan, avoid=fit) for i in range(self.V)]
            plans[key] = {"fit": fit, "check": check, "window_s": self.W}
        return plans[key]

    def _run(self, fit_starts=None, check_starts=None):
        K, W, V = self.K, self.W, self.V
        allines, lines = self.allines, self.lines
        D = self.index.duration()
        margin = 0.03 * D
        span = (D - 2 * margin) / K
        plan = self.plan(D, margin, span)
        if fit_starts is not None:      # a close call judged again with more windows
            plan = {"fit": list(fit_starts), "check": list(check_starts), "window_s": W}
            K = len(plan["fit"])
        self.prefetch(plan["fit"] + plan["check"], W)   # every first fit reads both: at once, not one by one
        windows = [self.read_window(start, W) for start in plan["fit"]]
        # the subtitle's signal as far as a window can reach: none starts past D, and one reads at most OMAX + W past
        # its start (one line ending at 99:59:59 made it millions of frames, per scale)
        n = int(min(max(l["e"] for l in allines) * 1.1 + 60, D + OMAX + 2 * W) / DT)
        fits, best = {}, 1.0    # scale: (offset, score, signal, curves), kept for 1.0 and the best scale so far
        for c in SCALES:        # 1.0 first
            ssig = self.sub_signal(lines, c, n)
            cs = [self.fast_corr(w, ssig) for w in windows]
            total = np.sum([v / self.frames(w) for w, (_, v) in zip(windows, cs)], axis=0)
            j = peak(total, 0.0)
            if c == 1.0 or float(total[j]) > fits[best][1]:
                if best != 1.0:
                    del fits[best]
                fits[c], best = (float(LAGS[j]), float(total[j]), ssig, cs), c
        c = best if fits[best][1] - fits[1.0][1] >= SCALE_GAIN * K else 1.0
        o_glob, _, ssig, curves = fits[c]

        def curve(w):
            return self.fast_corr(w, ssig)

        def label(w, cv=None):
            offs, vals = curve(w) if cv is None else cv
            j = peak(vals, o_glob)
            o_top, z_top = float(offs[j]), float(vals[j]) / self.frames(w)
            z_g = self.zat(w, (offs, vals), o_glob)
            if abs(o_top - o_glob) <= SAME_OFFSET_S:
                return o_glob if z_g >= FITS_Z else None
            if z_top - z_g >= OWN_GAIN_Z and z_top >= OWN_PEAK_Z:
                return o_top
            return None

        labels = [label(w, cv) for w, cv in zip(windows, curves)]   # the chosen scale's curves, not computed again
        for i, (w, lab) in enumerate(zip(list(windows), list(labels))):
            if lab is None or abs(lab - o_glob) <= SAME_OFFSET_S:
                continue
            same = [k for k in range(len(labels))
                    if labels[k] is not None and abs(labels[k] - lab) <= SAME_OFFSET_S and k != i]
            if not same:
                wc = self.read_window(w["start"] + W, W)
                windows.append(wc)
                lc = label(wc)
                if lc is None or abs(lc - lab) > SAME_OFFSET_S:
                    labels[i] = None
        seq = [(w["start"], lab) for w, lab in zip(windows[:K], labels) if lab is not None]
        bounds = []
        for (sa, oa), (sb, ob) in zip(seq, seq[1:]):
            if abs(oa - ob) <= SAME_OFFSET_S:
                continue
            lo, hi = sa + W, sb
            while hi - lo > W:
                mid = (lo + hi) / 2 - W / 2
                wm = self.read_window(mid, W)
                windows.append(wm)
                cv = curve(wm)
                za, zb = self.zat(wm, cv, oa), self.zat(wm, cv, ob)
                if max(za, zb) < FITS_Z:
                    break
                if za >= zb:
                    lo = mid + W
                else:
                    hi = mid
            bounds.append((lo, hi, oa, ob))
        order = sorted(range(len(allines)), key=lambda k: allines[k]["s"])
        lag = self.end_lag(c, o_glob, windows) if bounds else None
        cuts = []
        for lo, hi, oa, ob in bounds:
            best_k, best_score = None, -1
            for k in range(len(order) + 1):
                before = [allines[order[i]]["s"] * c + oa for i in range(k)]
                after = [allines[order[i]]["s"] * c + ob for i in range(k, len(order))]
                if before and before[-1] > hi + W:
                    break
                if after and after[0] < lo - W:
                    continue
                errs = self.onset_errors(before + after, windows)
                score = sum(1 for e in errs if e is not None and abs(e) < CUT_ERR_S)
                if lag is not None:     # the line ends too, against the subtitle's own lag (D3)
                    ends = [allines[order[i]]["e"] * c + oa for i in range(k)] + \
                           [allines[order[i]]["e"] * c + ob for i in range(k, len(order))]
                    score += END_WEIGHT * sum(1 for e in self.onset_errors(ends, windows, "offsets")
                                              if e is not None and abs(e - lag) < CUT_ERR_S)
                if score > best_score:
                    best_k, best_score = k, score
            if best_k is not None:
                cuts.append((best_k, oa, ob))
        seg_of = [None] * len(allines)
        cur = seq[0][1] if seq else o_glob
        ci = 0
        for rank, k in enumerate(order):
            while ci < len(cuts) and rank >= cuts[ci][0]:
                cur = cuts[ci][2]
                ci += 1
            seg_of[k] = cur
        dropped, last_end = set(), -1e9
        for k in order:
            l = allines[k]
            ms = l["s"] * c + seg_of[k]
            if l["kind"] == "dialogue" and ms < last_end - 0.5:
                dropped.add(k)
                continue
            if l["kind"] == "dialogue":
                last_end = max(last_end, l["e"] * c + seg_of[k])
        fine = {}
        for o in sorted(set(seg_of)):
            idx = [k for k in range(len(allines)) if seg_of[k] == o and allines[k]["kind"] == "dialogue"]
            errs = [e for e in self.onset_errors([allines[k]["s"] * c + o for k in idx], windows)
                    if e is not None and abs(e) < 0.5]
            fine[o] = float(np.median(errs)) if len(errs) >= 5 else 0.0
        new = [dict(l, s=l["s"] * c + seg_of[k] + fine[seg_of[k]], e=l["e"] * c + seg_of[k] + fine[seg_of[k]],
                    drop=k in dropped) for k, l in enumerate(allines)]
        fit_count = len(windows)
        vspan = (D - 2 * margin) / V
        verify = [self.read_window(start, W) for start in plan["check"]]
        dial_new = [l["s"] for l in new if l["kind"] == "dialogue" and not l["drop"]]
        extra = 0
        while sum(e is not None for e in self.onset_errors(dial_new, verify)) < GATE_LINES and extra < V:
            verify.append(self.read_window(max(0.0, margin + (extra + 0.5) * vspan + 3 * span / 4 - W / 2), W))
            extra += 1
        g_new = self.gate(self.onset_errors(dial_new, verify))
        g_old = self.gate(self.onset_errors([l["s"] for l in lines], windows + verify), strict_mode=True)
        shift = o_glob + fine.get(o_glob, 0.0) if len(set(seg_of)) == 1 else None
        agree = all(l is None or abs(l - o_glob) <= SAME_OFFSET_S for l in labels) \
            and sum(l is not None for l in labels) >= 0.6 * K
        small = c == 1.0 and shift is not None and abs(shift) <= 0.10  # below the bar's resolution: never publish
        moved = statistics.median(abs(n["s"] - l["s"]) for n, l in zip(new, allines)
                                  if l["kind"] == "dialogue" and not n["drop"]) if dial_new else 0.0
        why = None
        if g_old["pass"] or (small and agree and g_new["pass"]):
            decision = "in_sync"
        elif small and self.meets_better(g_old):   # made for this release, it fits the speech as it is
            decision, why = "in_sync", "fits the speech as it is"
        elif small:
            decision = "keep_original"
        elif g_new["pass"]:
            decision = "synced"
        elif self.clearly_better(g_new, moved):
            decision, why = "synced", "clearly better than the original"
        else:
            decision = "keep_original"
        return {"decision": decision, "why": why, "lines": new if decision == "synced" else None, "moved_s": round(moved, 2),
                "seq": seq, "segment_offsets": sorted(set(seg_of)), "end_lag_ms": None if lag is None else round(lag * 1000),
                "model": {"scale": round(c, 5), "offset": round(o_glob, 2), "lead_ms": round(self.lead * 1000),
                          "segments": [{"after_rank": k, "from": a, "to": b} for k, a, b in cuts],
                          "fine_ms": {str(k): round(v * 1000) for k, v in fine.items()}, "dropped": len(dropped)},
                "gate_new": g_new, "gate_old": g_old, "fit_windows": fit_count, "windows": self.windows_read, "plan": plan,
                "audio_s": self.audio_s}


def fit_tracks(index, lines, fit_class=None, original=None, **kw):
    """The fit on the file's audio tracks in track_order's order: with the title's original language known, its track
    first (a dub as the default is then fitted only if the original's track keeps the original); else the default
    track, and when that keeps the original and the file has a track in another language (a dual-audio release whose
    default is the dub), that one too. The first track that gives a result wins; the rule is the same. Windows of
    every track tried count toward the file's audio budget. A result on another track than the default names it."""
    fit_class = fit_class or AudioFit
    order = track_order(index, original)
    if not order:
        return fit_class(index, lines, **kw).run()
    tried, windows, audio_s = [], 0, 0
    for t in order:
        fit = fit_class(index, lines, **kw).run() if t.get("default") else \
            fit_class(index, lines, track=t["index"], **kw).run()
        windows += fit.get("windows") or 0
        audio_s += fit.get("audio_s") or 0
        if fit["decision"] != "keep_original":
            out = dict(fit, windows=windows, audio_s=audio_s) if tried else fit
            if not t.get("default"):
                out = dict(out, audio_track=t["index"], audio_language=t.get("language"))
            return out
        tried.append((t, fit))
    first_track, first = tried[0]
    if len(tried) == 1:
        return first
    other, why = tried[1]
    return dict(first, windows=windows, audio_s=audio_s,
                other_track={"index": other["index"], "language": other.get("language"), "why": why.get("why")})
