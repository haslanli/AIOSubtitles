"""Subtitle text: decode, parse (SRT, WebVTT, ASS/SSA) and write back with new timings.

ASS keeps its header, styles and every field but Start/End; songs and signs are told apart from dialogue by
style name (they move with their segment but are not part of the dialogue measure).
"""
import hashlib
import re

from . import srclimits

TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})|(\d{1,2}):(\d{2})[.,](\d{3})")


def decode(raw, lang=""):
    """Bytes to text: UTF-8 (BOM dropped), else cp1251 for Cyrillic languages, else latin-1."""
    for enc in ("utf-8-sig",):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    if lang[:2] in ("ru", "uk", "bg", "be", "sr", "mk"):
        try:
            return raw.decode("cp1251")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def _t(s):
    m = TIME.search(s)
    if not m:
        raise ValueError("no timestamp")
    if m.group(1) is not None:
        h, mi, se, frac = m.group(1, 2, 3, 4)
    else:
        h, (mi, se, frac) = "0", m.group(5, 6, 7)
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(frac.ljust(3, "0")) / 1000


def kind_of_style(style):
    style = style.strip().lower()
    if "sign" in style:
        return "sign"
    if style.startswith("song") or style in ("op", "ed") or style.startswith(("op ", "ed ", "op-", "ed-")):
        return "song"
    return "dialogue"


def is_ass(text):
    return "[Events]" in text


def parse(text):
    """List of dicts: s, e (seconds), text, kind ('dialogue'|'song'|'sign')."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    out = []
    if is_ass(text):
        fmt = None
        for line in text.split("\n"):
            if line.startswith("Format:") and fmt is None and "start" in line.lower() and "text" in line.lower():
                fmt = [x.strip().lower() for x in line[7:].split(",")]
            elif line.startswith("Dialogue:") and fmt:
                parts = line[9:].split(",", len(fmt) - 1)
                d = dict(zip(fmt, parts))
                try:
                    s, e = _t(d["start"]), _t(d["end"])
                except (KeyError, ValueError):
                    continue
                out.append({"s": s, "e": e, "text": d.get("text", ""), "kind": kind_of_style(d.get("style", ""))})
        return out
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        arrow = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if arrow is None:
            continue
        a, _, b = lines[arrow].partition("-->")
        try:
            s, e = _t(a), _t(b)
        except ValueError:
            continue
        out.append({"s": s, "e": e, "text": "\n".join(lines[arrow + 1:]), "kind": "dialogue"})
    return out


def dialogue_starts(lines):
    return [l["s"] for l in lines if l["kind"] == "dialogue"]


def dialogue_ends(lines):
    """The dialogue lines' ends, in dialogue_starts' order."""
    return [l["e"] for l in lines if l["kind"] == "dialogue"]


def ts(t):
    ms = max(0, int(round(t * 1000)))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def ass_ts(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def to_srt(lines):
    out, k = [], 0
    for l in lines:
        if l.get("drop"):
            continue
        k += 1
        body = re.sub(r"\{[^}]*\}", "", l["text"]).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        out.append(f"{k}\n{ts(l['s'])} --> {ts(l['e'])}\n{body.strip()}\n")
    return "\n".join(out)


def write_like(src_text, lines):
    """src_text re-timed with lines (same count and order as parse(src_text)); ASS keeps every other field,
    the header and the styles; SRT/WebVTT come back as SRT. Lines marked drop are left out."""
    text = src_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not is_ass(text):
        return to_srt(lines)
    it = iter(lines)
    res, fmt = [], None
    for line in text.split("\n"):
        if line.startswith("Format:") and fmt is None and "start" in line.lower() and "text" in line.lower():
            fmt = [x.strip().lower() for x in line[7:].split(",")]
        if line.startswith("Dialogue:") and fmt:
            parts = line[9:].split(",", len(fmt) - 1)
            d = dict(zip(fmt, parts))
            try:
                _t(d["start"]), _t(d["end"])
            except (KeyError, ValueError):
                res.append(line)
                continue
            l = next(it)
            if l.get("drop"):
                continue
            si, ei = fmt.index("start"), fmt.index("end")
            parts[si], parts[ei] = ass_ts(l["s"]), ass_ts(l["e"])
            res.append("Dialogue:" + ",".join(parts))
        else:
            res.append(line)
    return "\n".join(res)


def plain_srt(lines):
    """Every line, in file order, as a plain SRT block with placeholder text: the candidate as alass gets it
    (alass refuses WebVTT and SRT with a malformed index line; parse() reads both)."""
    out = []
    for i, l in enumerate(lines, 1):
        out.append(f"{i}\n{ts(l['s'])} --> {ts(max(l['e'], l['s'] + 0.001))}\nx\n")
    return "\n".join(out)


def timing_srt(starts_durations):
    """An SRT of timings only (the reference made from Matroska Cues)."""
    out = []
    for i, (s, d) in enumerate(sorted(starts_durations), 1):
        out.append(f"{i}\n{ts(s)} --> {ts(s + max(d, 0.5))}\nx\n")
    return "\n".join(out)


# a credit or advertising line a site or an uploader adds at the start or the end of a subtitle
CREDIT = re.compile(r"(?i)https?://|www\.|\.(com|org|net|info|tv|ru)\b|subtitles? (by|from)|downloaded from|"
                    r"sync(ed|hronized)? (and corrected )?by|corrected by|ripped by|translated by|opensubtitles|"
                    r"subdl|subsource|addic7ed|advertise")


def _plain(text):
    text = re.sub(r"\{[^}]*\}|<[^>]*>", "", text or "").replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return " ".join(text.split()).lower()


WEBVTT_NOTE = re.compile(r"(?im)^NOTE\b[ \t]*")


def fingerprint(text):
    """What makes two subtitles the same one: their dialogue lines' words and start times to a tenth of a
    second, once credit lines at the start and the end (at most three each) are left out, and for a subtitle of more
    than 20 lines its first and last three lines too: OpenSubtitles V3+ adds a notice line first and marks the first
    cue's lines with WebVTT's "NOTE". Another encoding, line ending, byte-order mark, format (SRT or WebVTT) or
    notice doesn't change it; another timing or text does."""
    lines = [l for l in parse(text) if l["kind"] == "dialogue"]
    for _ in range(3):
        if lines and CREDIT.search(_plain(lines[0]["text"])):
            lines = lines[1:]
    for _ in range(3):
        if lines and CREDIT.search(_plain(lines[-1]["text"])):
            lines = lines[:-1]
    if len(lines) > 20:
        lines = lines[srclimits.FINGERPRINT_EDGE:-srclimits.FINGERPRINT_EDGE]
    body = "\n".join(f"{round(l['s'], 1):.1f}|{_plain(WEBVTT_NOTE.sub('', l['text']))}" for l in lines)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _srt_text(text):
    """A line's text as to_srt writes it and parse reads it back (for matching a copy's lines to its original's)."""
    body = re.sub(r"\{[^}]*\}", "", text).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ").strip()
    return "\n".join(x for x in body.split("\n") if x.strip())


def timing_recipe(original, body):
    """How `body` was made from `original`: each original line's new start and end in milliseconds, or None
    for a line left out; {"same": True} when the copy is the original as it is. No text. None when `body` isn't a
    re-timing of `original` that apply_recipe rebuilds exactly."""
    if body == original:
        return {"v": 1, "same": True}
    src, out = parse(original), parse(body)
    ass = is_ass(original)
    norm = (lambda t: t) if ass else _srt_text
    times, j = [], 0
    for line in src:
        if j < len(out) and norm(line["text"]) == (out[j]["text"] if ass else _srt_text(out[j]["text"])):
            times.append([int(round(out[j]["s"] * 1000)), int(round(out[j]["e"] * 1000))])
            j += 1
        else:
            times.append(None)
    if j != len(out):
        return None
    recipe = {"v": 1, "times": times}
    return recipe if apply_recipe(original, recipe) == body else None


def apply_recipe(original, recipe):
    """The copy a timing recipe makes from its original, or None when the original doesn't fit it (another number
    of lines)."""
    if not isinstance(recipe, dict):
        return None
    if recipe.get("same"):
        return original
    times = recipe.get("times")
    src = parse(original)
    if not isinstance(times, list) or len(times) != len(src):
        return None
    lines = [dict(l, s=t[0] / 1000, e=t[1] / 1000) if t else dict(l, drop=True) for l, t in zip(src, times)]
    return write_like(original, lines)
