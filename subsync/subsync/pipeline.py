"""One (file, subtitle) sync: reference, alass sub-to-sub, per-line snap, the gate; below the bar the original is kept.

Reference, cheapest first: the release's own text track from the Matroska Cues (any language, not
forced/signs/songs, with at least half as many cues as the candidate has dialogue lines), else audio windows.
On the Cues route alass gets the candidate as a plain SRT of its parsed lines' timings (it refuses WebVTT and SRT with
a malformed index line), then each line can snap to its release line (refine.py). The snapped copy
must reach `snap_within` (95 %) within 200 ms, since snapping makes the gate read high; the alass copy needs the usual
90 %. The more exact copy that passes is offered.
A copy that misses those bars is still offered when it is clearly better than the original (relgate.clearly_better;
`better`, off unless given); the audio route has its own such rule (AudioFit, `audio_better`).
Outcomes: in_sync (the original passes as it is: nothing is published), synced (the re-timed copy passes and moved
lines by more than 100 ms), keep_original (anything else).
"""
import re
import statistics

from . import alass as alass_mod
from . import imageref, refine, shift
from .audiofit import fit_tracks
from .langs import lang3
from .relgate import aligned, clearly_better, relgate
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


SDH = re.compile(r"\b(sdh|cc|hearing[ -]?impaired|hard[ -]of[ -]hearing|hoh|deaf)\b", re.I)
# a track timed to a dub, not to the original audio (Spirited Away's "For Dub Audio": more lines, the dub's timing)
DUB = re.compile(r"\bdub(bed|titles?)?\b", re.I)

TYPESET_FRAME_S = 0.25     # a cue shown for less than this is a typesetting frame (fansub signs move frame by frame)
SAME_START_S = 0.02        # cues that start together are one line's layers
MAX_LINES_PER_MIN = 40     # dialogue is rarely denser than ~25 lines a minute; typesetting tracks run to thousands


def is_secondary(track):
    """Not the plain track of its language: one for the hearing impaired (it can trail the speech) or one timed
    to a dub."""
    name = track.get("name") or ""
    return bool(SDH.search(name) or DUB.search(name))


def dialogue_cues(cues):
    """A text track's cues as line (start, duration) pairs: typesetting frames left out, layers that start together
    counted once (the longest kept). A duration of 0 (not in the index) is kept as unknown."""
    out = []
    for s, d in sorted(cues):
        if 0 < d < TYPESET_FRAME_S:
            continue
        if out and s - out[-1][0] < SAME_START_S:
            out[-1] = (out[-1][0], max(out[-1][1], d))
            continue
        out.append((s, d))
    return out


def dialogue_like(cues):
    """Line starts as dense as dialogue can be (at most MAX_LINES_PER_MIN a minute over their span)."""
    if len(cues) < 2:
        return bool(cues)
    span = cues[-1][0] - cues[0][0]
    return span > 0 and len(cues) <= MAX_LINES_PER_MIN * span / 60


def text_references(index):
    """The file's text tracks with their cues as dialogue lines; tracks that are mostly typesetting (Frieren S01E01's
    Portuguese track: 55,735 cues in 24 minutes, against which any subtitle pairs by chance) are left out. Worked out
    once per file and kept on its index (every candidate of both languages asks)."""
    kept = getattr(index, "__dict__", {}).get("text_references")
    if kept is not None:
        return kept
    out = []
    for t in index.text_tracks():
        cues = dialogue_cues(t["cues"])
        if dialogue_like(cues):
            out.append(dict(t, cues=cues))
    if hasattr(index, "__dict__"):
        index.__dict__["text_references"] = out
    return out


FALLBACK_REFERENCE = ("eng",)


def image_reference(index, track):
    """imageref.reference of an image track, worked out once per file and kept on its index (every candidate asks)."""
    kept = getattr(index, "__dict__", {}).setdefault("image_references", {}) if hasattr(index, "__dict__") else {}
    if track["number"] not in kept:
        kept[track["number"]] = imageref.reference(track)
    return kept[track["number"]]


def choose_reference(index, n_dialogue, cand_lang, image_ref=False, prefer_plain=True, fallback=FALLBACK_REFERENCE):
    """A text track first; with image_ref, else an image track whose cue times read as subtitles. The
    returned track's "cues" are line (start, duration) pairs; "route" says which kind it is. With prefer_plain, a plain
    track of the language beats an SDH one (on Game of Thrones S01E01 the SDH track trails the speech). With no
    track in the subtitle's language, one in a `fallback` language comes first (Frieren's Russian was checked against a
    Portuguese track, a few lines longer than the English one AutoSync and the scoring use)."""
    want = lang3(cand_lang)
    fallback = {lang3(x) for x in fallback}
    usable = [t for t in text_references(index) if not t["forced"] and len(t["cues"]) >= 0.5 * n_dialogue]
    if usable:
        usable.sort(key=lambda t: (lang3(t["language"]) != want, lang3(t["language"]) not in fallback,
                                   prefer_plain and is_secondary(t), -len(t["cues"])))
        return dict(usable[0], route="cues")
    if not image_ref or not hasattr(index, "image_tracks"):
        return None
    images = []
    for t in index.image_tracks():
        if t["forced"]:
            continue
        ref = image_reference(index, t)
        # an image reference keeps only its sure starts: enough of them to pin the candidate's lines, else
        # the pairing minimums (scaled by this share) would get loose enough for chance pairs
        if ref["plausible"] and len(ref["cues"]) >= MIN_IMAGE_SHARE * n_dialogue:
            images.append(dict(t, cues=ref["cues"], image=ref["stats"], route="image"))
    if not images:
        return None
    images.sort(key=lambda t: (lang3(t["language"]) != want, lang3(t["language"]) not in fallback, -len(t["cues"])))
    return images[0]


OWN_TRACK_SHARE = 0.25   # a track with fewer lines than this share of a subtitle's is not its dialogue (signs, songs)


def own_track(index, n_dialogue, image_ref=False):
    """The file has a subtitle track of its own that this subtitle could be checked against, had it not more
    than twice that track's lines: a text track (or with image_ref an image track that reads as subtitles) with at least
    a quarter of its lines. Such a subtitle is kept; only a file with no such track needs the audio route."""
    if any(not t["forced"] and len(t["cues"]) >= OWN_TRACK_SHARE * n_dialogue for t in text_references(index)):
        return True
    if not image_ref or not hasattr(index, "image_tracks"):
        return False
    for t in index.image_tracks():
        if t["forced"]:
            continue
        ref = image_reference(index, t)
        if ref["plausible"] and len(ref["cues"]) >= OWN_TRACK_SHARE * 2 * MIN_IMAGE_SHARE * n_dialogue:
            return True
    return False


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


def reference_gate(ref, n_dialogue, splits=True):
    """The gate against a reference track: relgate with its line starts, its durations (for split lines) and the span
    rule. An image reference holds only the starts it is sure of, so even a perfect candidate pairs at most that share
    of its lines (Kingdom of Heaven's PGS track pins 73 % of its lines, and every Russian subtitle for this cut,
    with fewer lines than the track, was held to pairing all of them): the pairing minimums are scaled to it (recall)."""
    starts = [s for s, _ in ref["cues"]]
    durations = [d for _, d in ref["cues"]] if splits else None
    recall = 1.0
    if ref["route"] == "image":
        recall = min(1.0, len(starts) / n_dialogue, (ref.get("image") or {}).get("sure_share", 1.0))

    def gate(cand_starts, **kw):
        return relgate(starts, cand_starts, durations, min_span=MIN_SPAN, ref_recall=recall, **kw)
    return gate, recall


def snap_bar(ref, snap_within):
    """The share within 200 ms a snapped copy must reach: snapping makes the gate read high, the more so against an
    image reference's sparser starts."""
    return max(snap_within, IMAGE_SNAP_WITHIN) if ref["route"] == "image" else snap_within


def is_cross(ref, lang):
    """The reference is another language's track than the subtitle's."""
    return lang3(ref["language"]) != lang3(lang)


def original_fits(g, cross, better, recall):
    """An original in sync as it is: the gate, or against another language's track (with the rule) an alignment that is
    sound and needs no shift (its lines are not steadily off by more than MIN_SHIFT_S)."""
    if g["pass"]:
        return True
    if not (cross and better):
        return False
    return aligned(g, better, MIN_SPAN, recall)[0] and abs(g.get("signed_median_ms") or 0.0) <= MIN_SHIFT_S * 1000


def magnet(text, lines, ref, gate, snap_within, cross=False):
    """AutoSync's "magnet" for an original that is in sync as it is: its author still starts each line a little off the
    release's own line (Patriot S01E01's English: 43 ms early throughout; Burning: a stray line), so each dialogue line
    is snapped to the release line it clearly belongs to (refine.snap). Returns {"body", "stats"} when that moved a line
    and the snapped copy passes the snapped bar, else None (the original is listed as it is)."""
    snapped, stats = refine.snap(lines, ref["cues"])
    if all(abs(a["s"] - b["s"]) <= 0.001 for a, b in zip(lines, snapped)):
        return None
    g = gate(dialogue_starts(snapped), within_share=snap_bar(ref, snap_within))
    if cross:   # no per-line bar holds across languages: the snap must bring the lines closer to the reference
        before = gate(dialogue_starts(lines))
        if g.get("median_ms", 1e9) > before.get("median_ms", 0):
            return None
    elif not g["pass"]:
        return None
    return {"body": write_like(text, snapped), "stats": dict(stats, gate=g)}


def _in_sync(out, text, lines, ref, gate, snap_within, cross=False):
    """An original that passes as it is: with the magnet copy as its body when there is one."""
    m = magnet(text, lines, ref, gate, snap_within, cross) if snap_within else None
    return dict(out, body=m["body"], magnet=m["stats"]) if m else out


RECENTRE_MS = 20    # a copy offered unsnapped whose lines sit steadily more than this off the reference is moved back


def recentre(new, gate):
    """A re-timed copy moved by its own signed median error against the reference (alass can leave a steady bias:
    Kingdom of Heaven's Russian, -57 ms against its image track's starts), kept when that pairs no worse.
    Returns (lines, gate result, moved ms)."""
    g = gate(dialogue_starts(new))
    bias = (g.get("signed_median_ms") or 0.0) / 1000
    if abs(bias) * 1000 <= RECENTRE_MS:
        return new, g, 0.0
    moved = [dict(l, s=l["s"] - bias, e=l["e"] - bias) for l in new]
    g2 = gate(dialogue_starts(moved))
    if g2.get("median_ms", 1e9) <= g["median_ms"] and g2.get("within200", 0) >= g["within200"]:
        return moved, g2, round(-bias * 1000, 1)
    return new, g, 0.0


def judge_copy(out, text, lines, new, ref, gate, recall, snap=True, snap_within=0.95, better=None, how="", cross=False):
    """A re-timed copy (alass's, or one shift's) against the release track: passes as it is, or snapped to the release's
    lines, or as clearly better than the original; against another language's track (`cross`), when its alignment is
    sound (relgate.aligned), snapped onto that track's lines. Else the original is kept. `out` holds gate_original; `how`
    names the re-timing in the reason. A copy offered unsnapped is re-centred on the reference first."""
    dial = dialogue_starts(lines)
    new, g_new, moved_ms = recentre(new, gate)
    new_dial = dialogue_starts(new)
    out = dict(out, gate_synced=g_new, shift=_shift_stats(dial, new_dial))
    if moved_ms:
        out["recentred_ms"] = moved_ms
    best = new if out["gate_synced"]["pass"] else None
    snapped = None
    if snap:
        snapped, st = refine.snap(new, ref["cues"])
        snap_dial = dialogue_starts(snapped)
        out["gate_snapped"] = dict(gate(snap_dial, within_share=snap_bar(ref, snap_within)), **st)
        if out["gate_snapped"]["pass"]:
            best = snapped
            out["shift"] = _shift_stats(dial, snap_dial)
            out["refined"] = True
    why = "passes against the release track" + how + (" (snapped)" if out.get("refined") else "")
    if best is None and better is not None and cross:   # snapped onto the other language's lines, like AutoSync's magnet
        ok, note = aligned(out["gate_synced"], better, MIN_SPAN, recall)
        out["aligned"] = {"pass": ok, "why": note}
        if ok:
            best = snapped or new
            why = note + how + (" (snapped)" if snapped else "")
            out["shift"] = _shift_stats(dial, dialogue_starts(best))
    elif best is None and better is not None:
        ok, note = clearly_better(out["gate_original"], out["gate_synced"], better, MIN_SPAN, recall)
        out["better"] = {"pass": ok, "why": note}
        if ok:   # the re-timed copy, not the snapped one: the snapped copy's own measure reads high
            best, why = new, "clearly better than the original" + how
            out["shift"] = _shift_stats(dial, new_dial)
    if best is None:
        return dict(out, outcome="keep_original", reason="below the bar" + how)
    if out["shift"]["max_ms"] <= MIN_SHIFT_S * 1000:
        return dict(out, outcome="keep_original", reason="shift under 100 ms")
    return dict(out, outcome="synced", reason=why, body=write_like(text, best))


def shift_sync(index, text, ext, lang, snap=True, snap_within=0.95, splits=True, image_ref=False, better=None,
               prefer_plain=True, fallback=FALLBACK_REFERENCE):
    """The shift path: the subtitle moved by one steady offset (and a 24/25 fps scale) found from its line starts
    (shift.best_shift), judged like an alass copy. Milliseconds, no alass: the original's own check (check_original)
    comes first, alass later only where this keeps the original. Returns a sync_subtitle-like result, or None when there
    is no text or image reference or the subtitle can't be judged."""
    lines = parse(text)
    dial = dialogue_starts(lines)
    if len(dial) < MIN_LINES or broken_timing(lines):
        return None
    ref = choose_reference(index, len(dial), lang, image_ref, prefer_plain, fallback)
    if ref is None:
        return None
    gate, recall = reference_gate(ref, len(dial), splits)
    scale, offset, votes = shift.best_shift(dial, [s for s, _ in ref["cues"]])
    new = [dict(l, s=l["s"] * scale + offset, e=l["e"] * scale + offset) for l in lines]
    out = {"lines": len(lines), "dialogue": len(dial), "route": ref["route"], "gate_original": gate(dial),
           "reference": {"track": ref["number"], "language": ref["language"], "codec": ref["codec"], "cues": len(ref["cues"])},
           "shift_fit": {"scale": round(scale, 6), "offset_s": round(offset, 3), "votes": votes}}
    return judge_copy(out, text, lines, new, ref, gate, recall, snap, snap_within, better, how=" (one shift)",
                      cross=is_cross(ref, lang))


STACKED_OVERLAP_S = 0.5   # a line that starts this long before the previous one ends is on screen with it
STACKED_SHARE = 0.05      # best fit: a subtitle with this share of its lines stacked counts as STACKED_PENALTY less fit
STACKED_PENALTY = 0.05    # (of its share within 200 ms): after a plain one that fits about as well, not after a far worse one


def stacked_share(lines):
    """The share of dialogue lines that start while the previous line is still on screen (by STACKED_OVERLAP_S):
    another layer laid over the dialogue. The Fellowship of the Ring's fan subtitle with Elvish transcriptions: 7 %; a
    Russian subtitle of Inception with the English lines of a second track: 39 %; plain subtitles 0 to 1 %. Only the
    previous line counts, so one line with a wrong end time (minutes long) stacks one line, not all under it."""
    dial = sorted((l["s"], l["e"]) for l in lines if l["kind"] == "dialogue")
    stacked = sum(1 for (_, e0), (s, _) in zip(dial, dial[1:]) if s < e0 - STACKED_OVERLAP_S)
    return stacked / len(dial) if dial else 0.0


ANCHOR_SHARE = 0.3   # lines that must land on the main language's guess after one shift; chance is under 0.1


def anchored_guess(main_text, texts):
    """For a file with no subtitle track, before the audio route can check anything: of another language's subtitles
    (`texts`), the one that lines up best with the main language's best guess (a subtitle made for this release) after
    one shift (shift.best_shift), moved by it (Primer's YTS MP4: the Russian was 9 s off where the English rip fits).
    Returns {"index", "body", "share"} or None when none lines up (ANCHOR_SHARE of its lines)."""
    anchor = dialogue_starts(parse(main_text))
    if len(anchor) < MIN_LINES:
        return None
    best = None
    for i, text in enumerate(texts):
        lines = parse(text)
        dial = dialogue_starts(lines)
        if len(dial) < MIN_LINES or broken_timing(lines):
            continue
        scale, offset, votes = shift.best_shift(dial, anchor)
        share = votes / len(dial)
        if share >= ANCHOR_SHARE and (best is None or share > best["share"]):
            moved = [dict(l, s=l["s"] * scale + offset, e=l["e"] * scale + offset) for l in lines]
            best = {"index": i, "body": write_like(text, moved), "share": round(share, 3)}
    return best


def check_original(index, text, lang, splits=True, image_ref=False, prefer_plain=True, snap_within=None, better=None,
                   fallback=FALLBACK_REFERENCE):
    """The fast path: is this subtitle in sync as it is, against the release's own text (or image) track? A gate
    on the original only, no alass; in sync, with snap_within the magnet copy too (`body`, `magnet`). Returns
    {"in_sync": bool, ...} or None when there is no such reference (only the audio route could tell) or the subtitle
    can't be judged (too few lines, broken timing)."""
    lines = parse(text)
    dial = dialogue_starts(lines)
    if len(dial) < MIN_LINES or broken_timing(lines):
        return None
    ref = choose_reference(index, len(dial), lang, image_ref, prefer_plain, fallback)
    if ref is None:
        return None
    gate, recall = reference_gate(ref, len(dial), splits)
    g = gate(dial)
    cross = is_cross(ref, lang)
    fits = original_fits(g, cross, better, recall)
    reference = {"track": ref["number"], "language": ref["language"], "codec": ref["codec"], "cues": len(ref["cues"])}
    out = {"in_sync": fits, "route": ref["route"], "reference": reference, "gate_original": g,
           "lines": len(lines), "dialogue": len(dial), "cross": cross and not g["pass"]}
    return _in_sync(out, text, lines, ref, gate, snap_within, cross) if fits else out


def sync_subtitle(index, text, ext, lang, alass="alass", audio=True, audio_lead=0.19, vad=None, ffmpeg="ffmpeg",
                  force_route=None, snap=True, snap_within=0.95, splits=True, image_ref=False, audio_lock=None,
                  better=None, audio_better=None, audio_budget=None, prefer_plain=True, fallback=FALLBACK_REFERENCE):
    lines = parse(text)
    dial = dialogue_starts(lines)
    base = {"lines": len(lines), "dialogue": len(dial)}
    if len(dial) < MIN_LINES:
        return dict(base, outcome="keep_original", reason="too few dialogue lines", route=None)
    broken = broken_timing(lines)
    if broken:
        return dict(base, outcome="keep_original", reason=f"broken timing: {broken}", route=None)
    ref = None if force_route == "audio" else choose_reference(index, len(dial), lang, image_ref, prefer_plain, fallback)
    if ref is not None:
        gate, recall = reference_gate(ref, len(dial), splits)
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
        elif retimed is not None:
            out["alass_error"] = "line count changed"
        cross = is_cross(ref, lang)
        if original_fits(out["gate_original"], cross, better, recall):
            return _in_sync(dict(out, outcome="in_sync", reason="original passes against the release track",
                                 cross=cross and not out["gate_original"]["pass"]),
                            text, lines, ref, gate, snap_within if snap else None, cross)
        if new is None:
            return dict(out, outcome="keep_original", reason="alass failed")
        return judge_copy(out, text, lines, new, ref, gate, recall, snap, snap_within, better, cross=cross)

    if force_route != "audio" and own_track(index, len(dial), image_ref):
        return dict(base, outcome="keep_original", route=None,
                    reason="more than twice the lines of the file's own track: kept, never sent to the audio route")
    if not audio:
        return dict(base, outcome="keep_original", reason="no text track and the audio route is off", route=None)
    if audio_lock is not None:   # one audio fit at a time: it reads the file through a shared reader
        with audio_lock:
            fit = fit_tracks(index, lines, vad=vad, lead=audio_lead, ffmpeg=ffmpeg, better=audio_better,
                             budget=audio_budget)
    else:
        fit = fit_tracks(index, lines, vad=vad, lead=audio_lead, ffmpeg=ffmpeg, better=audio_better,
                         budget=audio_budget)
    out = dict(base, route="audio", model=fit.get("model"), gate_original=fit.get("gate_old"),
               gate_synced=fit.get("gate_new"), windows=fit.get("windows"), audio_s=fit.get("audio_s"))
    if fit.get("audio_track") is not None:    # the fit on another audio track than the default (a dub)
        out["audio_track"] = {"index": fit["audio_track"], "language": fit.get("audio_language")}
    if fit["decision"] == "in_sync":
        return dict(out, outcome="in_sync", reason=("original fits the speech as it is (release lead removed)" if fit.get("why")
                                                    else "original passes against speech (release lead removed)"))
    if fit["decision"] == "synced":
        reason = ("clearly better than the original (speech)" if fit.get("why")
                  else "passes against speech (release lead removed)")
        return dict(out, outcome="synced", reason=reason, body=write_like(text, fit["lines"]),
                    better={"pass": bool(fit.get("why")), "moved_s": fit.get("moved_s")})
    return dict(out, outcome="keep_original", reason=fit.get("why") or "below the bar or shift under 100 ms")
