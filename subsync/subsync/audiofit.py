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
"""
import bisect
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .langs import lang3
from .reader import Refused, Stopped
from .vad import SR, SileroVad

DT = 0.05
SCALES = [1.0, 25 / (24000 / 1001), (24000 / 1001) / 25, 24 / (24000 / 1001), (24000 / 1001) / 24, 25 / 24, 24 / 25]
OMAX = 600.0
MIN_W = 10.0        # shorter windows hold too few lines for the held-out check
PLANNED = 4         # windows a fit reads beyond its K + V (a confirmation, bisections, extra held-out windows)
MIN_WINDOW_LINES = 5  # a window whose usual place holds fewer of the subtitle's lines is moved to where they are
COST = 1.25         # a window's read costs about 1.25 x its length at the average byte rate (Cues, interleaving)
PARALLEL_READS = 4  # planned windows read at once (AudioFit.prefetch)


class AudioBudget(Exception):
    """The next audio window would pass the file's audio budget."""


class AudioFit:
    def __init__(self, index, lines, vad=None, k=8, w=30.0, v=4, lead=0.0, ffmpeg="ffmpeg", better=None, budget=None,
                 track=None):
        self.index, self.allines = index, lines
        self.track = track          # an audio track index (ffmpeg 0:a:N); None = the file's default track
        self.better = better
        self.budget = budget
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
        there. The same windows and bytes as reading them one by one. One prefetch per file at a time: the other
        language's fit (run alongside) then finds the windows read instead of reading them again."""
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
        pcm = (self.index.window_pcm(start, length, SR, self.ffmpeg) if self.track is None
               else self.index.window_pcm(start, length, SR, self.ffmpeg, track=self.track))
        with self._lock:
            self.windows_read += 1
            self.audio_s += length
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        fl = SR // 100  # 10 ms frames
        mask = np.zeros(len(audio) // fl, dtype=bool)
        for a, b in self.vad.speech(audio):
            mask[a // 160: b // 160] = True
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
            if merged and a - merged[-1][1] < 6:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        merged = [r for r in merged if r[1] - r[0] >= 10]
        mask[:] = False
        for a, b in merged:
            mask[a:b] = True
        onsets = np.array([start + a / 100 - self.lead for a, _ in merged])
        return {"start": start, "len": length, "mask": mask, "onsets": onsets}

    # -- correlation -------------------------------------------------------------------------------------------
    @staticmethod
    def sub_signal(lines, scale, n):
        s = -np.ones(n)
        for l in lines:
            a, b = int(l["s"] * scale / DT), int(l["e"] * scale / DT)
            s[max(a, 0):max(min(b, n), 0)] = 1
        return s

    @staticmethod
    def fast_corr(w, ssig):
        v = np.where(w["mask"][::5], 1.0, -1.0)
        k0 = int(round(w["start"] / DT))
        pad = int(OMAX / DT)
        s = np.concatenate([-np.ones(pad), ssig, -np.ones(pad + len(v))])
        n = len(s) + len(v)
        full = np.fft.irfft(np.fft.rfft(s, n) * np.conj(np.fft.rfft(v, n)), n)
        offs = np.arange(-pad, pad + 1)
        idx = k0 - offs + pad
        ok = (idx >= 0) & (idx < len(full))
        vals = np.full(len(offs), -np.inf)
        vals[ok] = full[idx[ok]]
        return offs * DT, vals

    @staticmethod
    def zat(w, curve, o):
        offs, vals = curve
        return float(vals[int(np.argmin(np.abs(offs - o)))]) / len(w["mask"][::5])

    @staticmethod
    def onset_errors(starts, windows):
        """Signed (onset - start) per mapped start inside a read window, matched one-to-one; None = no onset."""
        errs = []
        for w in windows:
            inside = [s for s in starts if w["start"] + 0.5 <= s <= w["start"] + w["len"] - 0.5]
            pairs = sorted((abs(o - s), i, j) for i, s in enumerate(inside) for j, o in enumerate(w["onsets"])
                           if abs(o - s) <= 1.0)
            used_l, used_o, got = set(), set(), {}
            for _, i, j in pairs:
                if i in used_l or j in used_o:
                    continue
                used_l.add(i)
                used_o.add(j)
                got[i] = float(w["onsets"][j] - inside[i])
            errs += [got.get(i) for i in range(len(inside))]
        return errs

    @staticmethod
    def gate(errs, need=25, strict_mode=False):
        matched = [abs(e) * 1000 for e in errs if e is not None]
        total = len(errs)
        if len(matched) < need:
            return {"n": total, "matched": len(matched), "pass": False, "why": "too few sampled lines"}
        med = float(np.median(matched))
        share = sum(x <= 200 for x in matched) / len(matched)
        miss = 1 - len(matched) / total
        strict = sum(x <= 200 for x in matched) / total
        return {"n": total, "matched": len(matched), "miss": round(miss, 3), "median_ms": round(med, 1),
                "within200": round(share, 3), "within200_misses_as_fail": round(strict, 3),
                "pass": med <= 100 and share >= 0.90 and miss <= 0.30 and (strict >= 0.90 if strict_mode else strict >= 0.80)}

    def meets_better(self, g):
        """The numbers of the second way to pass: matched lines, unmatched share, median error, within 200 ms."""
        b = self.better
        if not b or g.get("median_ms") is None:
            return False
        return (g["matched"] >= b["matched"] and g["miss"] <= b["miss"] and g["median_ms"] <= b["median_ms"]
                and g["within200"] >= b["within"])

    def clearly_better(self, g_new, moved):
        return self.meets_better(g_new) and moved >= self.better["min_shift_s"]

    # -- the fit -----------------------------------------------------------------------------------------------
    def run(self):
        """Returns {decision: in_sync|synced|keep_original, lines (new, for synced), model, gates, windows}."""
        if not self.index.duration() or not self.lines:
            return {"decision": "keep_original", "why": "no duration or no dialogue"}
        if self.budget and self.rate():
            w = self.budget / ((self.K + self.V + PLANNED) * COST * self.rate())
            if w < MIN_W:
                return {"decision": "keep_original", "windows": 0, "audio_s": 0.0,
                        "why": f"the file's bitrate is too high for the audio budget ({self.budget / 1e6:.0f} MB)"}
            self.W = min(self.W, round(w, 1))
        try:
            return self._run()
        except AudioBudget:
            return {"decision": "keep_original", "windows": self.windows_read, "audio_s": self.audio_s,
                    "why": f"audio budget reached ({self.budget / 1e6:.0f} MB)"}

    def place(self, usual, lo, hi, avoid=()):
        """A window's start: its usual place when the subtitle has at least MIN_WINDOW_LINES lines there; else, in
        [lo, hi - W], the start whose window holds the most of them (2 s steps). Never overlapping a window starting in
        `avoid`. (Moving every window to its stretch's busiest spot does worse on talkative films: rapid exchanges pair
        less surely with speech onsets.)"""
        W = self.W
        starts = sorted(l["s"] for l in self.lines)

        def lines_in(t):
            return bisect.bisect_left(starts, t + W) - bisect.bisect_left(starts, t)

        def free(t):
            return all(abs(t - a) >= W for a in avoid)
        if free(usual) and lines_in(usual) >= MIN_WINDOW_LINES:
            return usual
        best, best_n = usual, lines_in(usual) if free(usual) else -1
        t = lo
        while t + W <= hi + 1e-6:
            if free(t) and lines_in(t) > best_n:
                best, best_n = t, lines_in(t)
            t += 2.0
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

    def _run(self):
        K, W, V = self.K, self.W, self.V
        allines, lines = self.allines, self.lines
        D = self.index.duration()
        margin = 0.03 * D
        span = (D - 2 * margin) / K
        plan = self.plan(D, margin, span)
        self.prefetch(plan["fit"] + plan["check"], W)   # every first fit reads both: at once, not one by one
        windows = [self.read_window(start, W) for start in plan["fit"]]
        n = int((max(l["e"] for l in allines) * 1.1 + 60) / DT)
        res = {}
        for c in SCALES:
            ssig = self.sub_signal(lines, c, n)
            cs = [self.fast_corr(w, ssig) for w in windows]
            total = np.sum([v / len(w["mask"][::5]) for w, (_, v) in zip(windows, cs)], axis=0)
            j = int(np.argmax(total))
            res[c] = (float(cs[0][0][j]), float(total[j]), ssig)
        c = max(res, key=lambda k: res[k][1])
        if c != 1.0 and res[c][1] - res[1.0][1] < 0.1 * K:
            c = 1.0
        o_glob, _, ssig = res[c]

        def curve(w):
            return self.fast_corr(w, ssig)

        def label(w):
            offs, vals = curve(w)
            L = len(w["mask"][::5])
            j = int(np.argmax(vals))
            o_top, z_top = float(offs[j]), float(vals[j]) / L
            z_g = self.zat(w, (offs, vals), o_glob)
            if abs(o_top - o_glob) <= 0.5:
                return o_glob if z_g >= 0.3 else None
            if z_top - z_g >= 0.15 and z_top >= 0.45:
                return o_top
            return None

        labels = [label(w) for w in windows]
        for i, (w, lab) in enumerate(zip(list(windows), list(labels))):
            if lab is None or abs(lab - o_glob) <= 0.5:
                continue
            same = [k for k in range(len(labels)) if labels[k] is not None and abs(labels[k] - lab) <= 0.5 and k != i]
            if not same:
                wc = self.read_window(w["start"] + W, W)
                windows.append(wc)
                lc = label(wc)
                if lc is None or abs(lc - lab) > 0.5:
                    labels[i] = None
        seq = [(w["start"], lab) for w, lab in zip(windows[:K], labels) if lab is not None]
        bounds = []
        for (sa, oa), (sb, ob) in zip(seq, seq[1:]):
            if abs(oa - ob) <= 0.5:
                continue
            lo, hi = sa + W, sb
            while hi - lo > W:
                mid = (lo + hi) / 2 - W / 2
                wm = self.read_window(mid, W)
                windows.append(wm)
                cv = curve(wm)
                za, zb = self.zat(wm, cv, oa), self.zat(wm, cv, ob)
                if max(za, zb) < 0.3:
                    break
                if za >= zb:
                    lo = mid + W
                else:
                    hi = mid
            bounds.append((lo, hi, oa, ob))
        order = sorted(range(len(allines)), key=lambda k: allines[k]["s"])
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
                score = sum(1 for e in errs if e is not None and abs(e) < 0.4)
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
        while sum(e is not None for e in self.onset_errors(dial_new, verify)) < 25 and extra < V:
            verify.append(self.read_window(max(0.0, margin + (extra + 0.5) * vspan + 3 * span / 4 - W / 2), W))
            extra += 1
        g_new = self.gate(self.onset_errors(dial_new, verify))
        g_old = self.gate(self.onset_errors([l["s"] for l in lines], windows + verify), strict_mode=True)
        shift = o_glob + fine.get(o_glob, 0.0) if len(set(seg_of)) == 1 else None
        agree = all(l is None or abs(l - o_glob) <= 0.5 for l in labels) and sum(l is not None for l in labels) >= 0.6 * K
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
                "model": {"scale": round(c, 5), "offset": round(o_glob, 2), "lead_ms": round(self.lead * 1000),
                          "segments": [{"after_rank": k, "from": a, "to": b} for k, a, b in cuts],
                          "fine_ms": {str(k): round(v * 1000) for k, v in fine.items()}, "dropped": len(dropped)},
                "gate_new": g_new, "gate_old": g_old, "fit_windows": fit_count, "windows": self.windows_read, "plan": plan,
                "audio_s": self.audio_s}


def fit_tracks(index, lines, fit_class=None, **kw):
    """The fit on the file's default audio track; when that keeps the original and the file has an audio
    track in another language (a dual-audio release whose default is the dub), the fit on that track too. The first track
    that gives a result wins; the rule is the same. Windows of both count toward the file's audio budget."""
    fit_class = fit_class or AudioFit
    fit = fit_class(index, lines, **kw).run()
    if fit["decision"] != "keep_original" or not hasattr(index, "audio_tracks"):
        return fit
    tracks = index.audio_tracks() or []
    default = next((t for t in tracks if t.get("default")), tracks[0] if tracks else None)
    other = next((t for t in tracks if default is not None and t is not default
                  and lang3(t.get("language")) != lang3(default.get("language"))), None)
    if other is None:
        return fit
    second = fit_class(index, lines, track=other["index"], **kw).run()
    second["windows"] = (second.get("windows") or 0) + (fit.get("windows") or 0)
    second["audio_s"] = (second.get("audio_s") or 0) + (fit.get("audio_s") or 0)
    if second["decision"] == "keep_original":
        return dict(fit, windows=second["windows"], audio_s=second["audio_s"],
                    other_track={"index": other["index"], "language": other.get("language"), "why": second.get("why")})
    return dict(second, audio_track=other["index"], audio_language=other.get("language"))
