"""One (file, subtitle) sync: reference, alass sub-to-sub, per-line snap, the gate; below the bar the original is kept.

Reference, cheapest first: the release's own text track from the Matroska Cues (any language, not
forced/signs/songs, with at least half as many cues as the candidate has dialogue lines), else audio windows.
On the Cues route alass gets the candidate as a plain SRT of its parsed lines' timings (it refuses WebVTT and SRT with
a malformed index line), then each line can snap to its release line (refine.py). The snapped copy
must reach `snap_within` (95 %) within 200 ms, since snapping makes the gate read high; the alass copy needs the usual
90 %. The more exact copy that passes is offered.
Outcomes: in_sync (the original passes as it is: nothing is published), synced (the re-timed copy passes and moved
lines by more than 100 ms), keep_original (anything else).
"""
import statistics

from . import alass as alass_mod
from . import imageref, refine
from .audiofit import AudioFit
from .langs import lang3
from .relgate import relgate
from .subs import dialogue_starts, parse, plain_srt, timing_srt, write_like

MIN_LINES = 10
MIN_SHIFT_S = 0.100
MIN_SPAN = 0.70          # a candidate must cover >= 70 % of the release track's span
MIN_IMAGE_SHARE = 0.40   # an image reference needs sure starts for >= 40 % of the candidate's lines
IMAGE_SNAP_WITHIN = 0.97 # a snapped copy against an image reference reads higher still (sparser reference):
                         # on 72 synthetic cases 0.95 offered 7 copies under the true bar, 0.97 none


def broken_timing(lines):
    """Early reject: timings that can't be a film's or an episode's, so alass is not even tried
    (Spirited Away's English 2: every start under 0.03 s)."""
    d = [l for l in lines if l["kind"] == "dialogue"]
    if len(d) < MIN_LINES:
        return None
    starts = sorted(l["s"] for l in d)
    if starts[-1] - starts[0] < 60:
        return "every line within one minute"
    if sum(1 for l in d if l["e"] - l["s"] <= 0.01) > 0.5 * len(d):
        return "most lines have no duration"
    return None


def choose_reference(index, n_dialogue, cand_lang, image_ref=False):
    """A text track first; with image_ref, else an image track whose cue times read as subtitles. The
    returned track's "cues" are line (start, duration) pairs; "route" says which kind it is."""
    want = lang3(cand_lang)
    usable = [t for t in index.text_tracks() if not t["forced"] and len(t["cues"]) >= 0.5 * n_dialogue]
    if usable:
        usable.sort(key=lambda t: (lang3(t["language"]) != want, -len(t["cues"])))
        return dict(usable[0], route="cues")
    if not image_ref or not hasattr(index, "image_tracks"):
        return None
    images = []
    for t in index.image_tracks():
        if t["forced"]:
            continue
        ref = imageref.reference(t)
        # an image reference keeps only its sure starts: enough of them to pin the candidate's lines, else
        # the pairing minimums (scaled by this share) would get loose enough for chance pairs
        if ref["plausible"] and len(ref["cues"]) >= MIN_IMAGE_SHARE * n_dialogue:
            images.append(dict(t, cues=ref["cues"], image=ref["stats"], route="image"))
    if not images:
        return None
    images.sort(key=lambda t: (lang3(t["language"]) != want, -len(t["cues"])))
    return images[0]


def image_check(text_tracks, image_tracks):
    """Calibration of image references, at no read cost: on a file with a text track and an image track in the same language,
    how well the image track's derived line starts match the text track's (recorded in the file's detail)."""
    out = []
    for im in image_tracks:
        if im["forced"]:
            continue
        txt = [t for t in text_tracks if not t["forced"] and lang3(t["language"]) == lang3(im["language"]) and t["cues"]]
        if not txt:
            continue
        t = max(txt, key=lambda x: len(x["cues"]))
        ref = imageref.reference(im)
        g = relgate([s for s, _ in t["cues"]], [s for s, _ in ref["cues"]], [d for _, d in t["cues"]])
        out.append({"image_track": im["number"], "text_track": t["number"], "language": im["language"],
                    "plausible": ref["plausible"], **ref["stats"],
                    "vs_text": {k: g.get(k) for k in ("paired", "median_ms", "within200", "weak_tenths", "pass")}})
    return out[:4]


def _shift_stats(before, after):
    d = [abs(b - a) for a, b in zip(before, after)]
    return {"median_ms": round(statistics.median(d) * 1000, 1) if d else 0.0,
            "max_ms": round(max(d) * 1000, 1) if d else 0.0}


def check_original(index, text, lang, splits=True, image_ref=False):
    """The fast path: is this subtitle in sync as it is, against the release's own text (or image) track? A gate
    on the original only, no alass. Returns {"in_sync": bool, ...} or None when there is no such reference (only the
    audio route could tell) or the subtitle can't be judged (too few lines, broken timing)."""
    lines = parse(text)
    dial = dialogue_starts(lines)
    if len(dial) < MIN_LINES or broken_timing(lines):
        return None
    ref = choose_reference(index, len(dial), lang, image_ref)
    if ref is None:
        return None
    ref_starts = [s for s, _ in ref["cues"]]
    recall = min(1.0, len(ref_starts) / len(dial)) if ref["route"] == "image" else 1.0
    g = relgate(ref_starts, dial, [d for _, d in ref["cues"]] if splits else None, min_span=MIN_SPAN, ref_recall=recall)
    reference = {"track": ref["number"], "language": ref["language"], "codec": ref["codec"], "cues": len(ref["cues"])}
    return {"in_sync": bool(g["pass"]), "route": ref["route"], "reference": reference, "gate_original": g,
            "lines": len(lines), "dialogue": len(dial)}


def sync_subtitle(index, text, ext, lang, alass="alass", audio=True, audio_lead=0.19, vad=None, ffmpeg="ffmpeg",
                  force_route=None, snap=True, snap_within=0.95, splits=True, image_ref=False, audio_lock=None):
    lines = parse(text)
    dial = dialogue_starts(lines)
    base = {"lines": len(lines), "dialogue": len(dial)}
    if len(dial) < MIN_LINES:
        return dict(base, outcome="keep_original", reason="too few dialogue lines", route=None)
    broken = broken_timing(lines)
    if broken:
        return dict(base, outcome="keep_original", reason=f"broken timing: {broken}", route=None)
    ref = None if force_route == "audio" else choose_reference(index, len(dial), lang, image_ref)
    if ref is not None:
        ref_starts = [s for s, _ in ref["cues"]]
        ref_durs = [d for _, d in ref["cues"]] if splits else None
        # an image reference holds only the starts it is sure of: scale the pairing minimums to it
        recall = min(1.0, len(ref_starts) / len(dial)) if ref["route"] == "image" else 1.0
        gate = lambda starts, **kw: relgate(ref_starts, starts, ref_durs, min_span=MIN_SPAN, ref_recall=recall, **kw)  # noqa: E731
        out = dict(base, route=ref["route"], reference={"track": ref["number"], "language": ref["language"],
                                                         "codec": ref["codec"], "cues": len(ref["cues"])})
        if ref.get("image"):
            out["reference"]["image"] = ref["image"]
        out["gate_original"] = gate(dial)
        try:   # every line, signs and songs too, so the count and order match parse(text)
            retimed = parse(alass_mod.run(timing_srt(ref["cues"]), plain_srt(lines), "srt", alass=alass))
        except (alass_mod.AlassFailed, OSError) as e:
            retimed = None
            out["alass_error"] = str(e)
        new = None
        if retimed is not None and len(retimed) == len(lines):
            new = [dict(l, s=r["s"], e=r["e"]) for l, r in zip(lines, retimed)]
            new_dial = dialogue_starts(new)
            out["gate_synced"] = gate(new_dial)
            out["shift"] = _shift_stats(dial, new_dial)
        elif retimed is not None:
            out["alass_error"] = "line count changed"
        if out["gate_original"]["pass"]:
            return dict(out, outcome="in_sync", reason="original passes against the release track")
        if new is None:
            return dict(out, outcome="keep_original", reason="alass failed")
        best = new if out["gate_synced"]["pass"] else None
        if snap:
            snapped, st = refine.snap(new, ref["cues"])
            snap_dial = dialogue_starts(snapped)
            need = max(snap_within, IMAGE_SNAP_WITHIN) if ref["route"] == "image" else snap_within
            out["gate_snapped"] = dict(gate(snap_dial, within_share=need), **st)
            if out["gate_snapped"]["pass"]:
                best = snapped
                out["shift"] = _shift_stats(dial, snap_dial)
                out["refined"] = True
        if best is None:
            return dict(out, outcome="keep_original", reason="below the bar")
        if out["shift"]["max_ms"] <= MIN_SHIFT_S * 1000:
            return dict(out, outcome="keep_original", reason="shift under 100 ms")
        return dict(out, outcome="synced", reason="passes against the release track" + (" (snapped)" if out.get("refined") else ""),
                    body=write_like(text, best))
    if not audio:
        return dict(base, outcome="keep_original", reason="no text track and the audio route is off", route=None)
    if audio_lock is not None:   # one audio fit at a time: it reads the file through a shared reader
        with audio_lock:
            fit = AudioFit(index, lines, vad=vad, lead=audio_lead, ffmpeg=ffmpeg).run()
    else:
        fit = AudioFit(index, lines, vad=vad, lead=audio_lead, ffmpeg=ffmpeg).run()
    out = dict(base, route="audio", model=fit.get("model"), gate_original=fit.get("gate_old"),
               gate_synced=fit.get("gate_new"), windows=fit.get("windows"), audio_s=fit.get("audio_s"))
    if fit["decision"] == "in_sync":
        return dict(out, outcome="in_sync", reason="original passes against speech (release lead removed)")
    if fit["decision"] == "synced":
        return dict(out, outcome="synced", reason="passes against speech (release lead removed)",
                    body=write_like(text, fit["lines"]))
    return dict(out, outcome="keep_original", reason=fit.get("why") or "below the bar or shift under 100 ms")
