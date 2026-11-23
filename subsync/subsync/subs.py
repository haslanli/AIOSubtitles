"""Subtitle text: decode, parse (SRT, WebVTT, ASS/SSA) and write back with new timings.

ASS keeps its header, styles and every field but Start/End; songs and signs are told apart from dialogue by the words
of their style name (they move with their segment but are not part of the dialogue measure). SRT and WebVTT come back
as SRT: an SRT's text as it is, WebVTT's (and ASS's, for a player that asks for SRT) as SRT writes it (to_srt).
"""
import codecs
import hashlib
import re
from html import unescape
from typing import NamedTuple

from . import langs, srclimits

# A time starts at no digit inside a run of digits: a search over a long run would otherwise start at each of them
# (quadratic time)
TIME = re.compile(r"(?<!\d)(\d+):(\d{2}):(\d{2})[.,](\d{1,3})|(?<!\d)(\d{1,2}):(\d{2})[.,](\d{3})")
MAX_HOUR_DIGITS = 4   # a time with more is none: no line starts 10,000 hours in (and ~300 digits overflow a float)


# byte-order marks, UTF-32's first (its little-endian mark starts with UTF-16's)
BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"), (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))
# UTF-16 without a mark: in its first SNIFF_BYTES, zeros at no even place and at least UTF16_ZEROS of the odd ones
# (little-endian), or the reverse (big-endian)
SNIFF_BYTES = 1024
UTF16_ZEROS = 0.25
UTF8_BAD_SHARE = 0.001      # a UTF-8 file with up to this share of bad bytes is still UTF-8 (each one read as U+FFFD)
# the most common characters of Chinese dialogue, in both their simplified and traditional forms: a Chinese subtitle
# read in the wrong one of GBK and Big5 comes out as rare characters
COMMON_CHINESE = frozenset("我你的是了不他在这這个個有们們就要什么麼好那说說到会會去来來一人知道吗嗎吧啊呢想看没沒也都能"
                           "为為对對和她上时時让讓给給怎样樣很还還事做走再可以但把得过過下自己大小现現真谢謝先生别別快"
                           "天中家点點出开開回吃听聽跟谁誰太多些")


def _utf16_without_bom(raw):
    """"utf-16-le" or "utf-16-be" for text that is UTF-16 without a byte-order mark (a zero at every other byte), else
    None."""
    head = raw[:SNIFF_BYTES]
    even, odd = head[0::2], head[1::2]
    for zeros, other, enc in ((odd, even, "utf-16-le"), (even, odd, "utf-16-be")):
        if zeros and not other.count(0) and zeros.count(0) >= UTF16_ZEROS * len(zeros):
            return enc
    return None


def _utf8_with_stray_bytes(raw):
    """A UTF-8 file's text with its few bad bytes (UTF8_BAD_SHARE at most) each as U+FFFD, or None. A file whose other
    characters beyond ASCII are fewer than its bad bytes is no UTF-8 file (one in cp1252 with a few curly quotes)."""
    text = raw.decode("utf-8", "replace")
    bad = text.count("\ufffd") - raw.count("\ufffd".encode("utf-8"))
    beyond_ascii = sum(1 for ch in text if ch > "\x7f") - text.count("\ufffd")
    return text if bad <= UTF8_BAD_SHARE * len(raw) and beyond_ascii > bad else None


def _legacy(raw, lang):
    """A subtitle that isn't Unicode, in its language's usual Windows encoding (langs.legacy_encodings): Serbian as
    Central European (cp1250) when most of its letters are plain Latin, else as Cyrillic (cp1251); Chinese as GBK or
    Big5, whichever gives more common Chinese characters; any other language Western (cp1252). Latin-1 last, when the
    encoding can't read it."""
    encodings = langs.legacy_encodings(lang)
    if langs.lang3(lang) == "srp":
        latin, cyrillic = encodings
        plain = sum(1 for b in raw if 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A)
        encodings = (latin,) if plain > sum(1 for b in raw if b >= 0x80) else (cyrillic,)
    elif len(encodings) == 2:   # Chinese
        texts = [raw.decode(enc, "replace") for enc in encodings]
        return max(texts, key=lambda t: sum(1 for ch in t if ch in COMMON_CHINESE))
    for enc in (*encodings, "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def decode(raw, lang=""):
    """Bytes to text: by its byte-order mark (UTF-8, UTF-16, UTF-32), as UTF-16 without one, as UTF-8 (also
    with a stray byte or two), else in the language's usual Windows encoding (_legacy). The same subtitle in two
    encodings reads the same, so it is one subtitle (fingerprint)."""
    for bom, enc in BOMS:
        if raw.startswith(bom):
            return raw.decode(enc, "replace")
    enc = _utf16_without_bom(raw)
    if enc:
        return raw.decode(enc, "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return _utf8_with_stray_bytes(raw) or _legacy(raw, lang)


def _t(s):
    m = TIME.search(s)
    if not m:
        raise ValueError("no timestamp")
    if m.group(1) is not None:
        h, mi, se, frac = m.group(1, 2, 3, 4)
        if len(h) > MAX_HOUR_DIGITS:
            raise ValueError("no timestamp")
    else:
        h, (mi, se, frac) = "0", m.group(5, 6, 7)
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(frac.ljust(3, "0")) / 1000


# the words of an ASS style name that make its lines songs or signs, whole words only: "OP_Romaji",
# "ED-English", "Opening", "Insert Song" and "Karaoke" are songs, "TS" and "Title" signs, "Design" dialogue
SONG_WORDS = frozenset(("op", "ed", "opening", "ending", "song", "songs", "insert", "karaoke", "kara", "lyrics"))
SIGN_WORDS = frozenset(("sign", "signs", "ts", "title", "typeset"))
# a style name's words: runs of letters, split before a capital that starts a word ("OPRomaji": OP, Romaji)
STYLE_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def kind_of_style(style):
    """'sign', 'song' or 'dialogue': by the style name's words (SIGN_WORDS, SONG_WORDS)."""
    words = {w.lower() for w in STYLE_WORD.findall(style)}
    if words & SIGN_WORDS:
        return "sign"
    if words & SONG_WORDS:
        return "song"
    return "dialogue"


def is_ass(text):
    return "[Events]" in text


def _normalised(text):
    """text with "\\n" line ends and no byte-order mark in front."""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")


class _Event(NamedTuple):
    """An ASS Dialogue line whose Start and End can be read."""
    s: float
    e: float
    fields: dict    # by the Format line's names, lower case
    parts: list     # the fields as written, in order (the last one, Text, keeps its commas)
    start_at: int   # Start's and End's places in parts
    end_at: int

    def line(self):
        """The line as parse() gives it."""
        return {"s": self.s, "e": self.e, "text": self.fields.get("text", ""),
                "kind": kind_of_style(self.fields.get("style", ""))}

    def kept(self):
        """What a re-timing leaves as it is: every field but Start and End."""
        return tuple(p for k, p in enumerate(self.parts) if k not in (self.start_at, self.end_at))


def _ass_lines(text):
    """Each line of a normalised ASS text with its event (_Event), or None for a line that is no Dialogue line or whose
    Start or End can't be read. parse() and write_like() both read the events here, so they agree on which lines count
    and in what order."""
    fmt = at = None
    for line in text.split("\n"):
        event = None
        if fmt is None and line.startswith("Format:") and "start" in line.lower() and "text" in line.lower():
            fmt = [x.strip().lower() for x in line[7:].split(",")]
            at = {name: fmt.index(name) for name in ("start", "end") if name in fmt}
        elif fmt and line.startswith("Dialogue:"):
            parts = line[9:].split(",", len(fmt) - 1)
            fields = dict(zip(fmt, parts))
            try:
                event = _Event(_t(fields["start"]), _t(fields["end"]), fields, parts, at["start"], at["end"])
            except (KeyError, ValueError):
                pass
        yield line, event


def _cues(text):
    """The cues of a normalised SRT or WebVTT text, read line by line: every line holding a valid
    "time --> time" starts one, also with no blank line before it (a number on the line just before it is then its
    index, not text), and its text runs to the next such line or a blank line. Lines in no cue (WebVTT's header, notes
    and styles, an index, a cue's identifier) are left out."""
    out, cue = [], None    # cue: the one being read, (start, end, its text lines), None after a blank line
    for line in text.strip().split("\n"):
        if not line.strip():
            cue = None
            continue
        a, arrow, b = line.partition("-->")
        if arrow:
            try:
                s, e = _t(a), _t(b)
            except ValueError:
                pass
            else:
                # an index: the digits TIME reads (\d, isdecimal), never a superscript isdigit also takes
                if cue is not None and cue[2] and cue[2][-1].strip().isdecimal():
                    cue[2].pop()
                cue = (s, e, [])
                out.append(cue)
                continue
        if cue is not None:
            cue[2].append(line)
    return [{"s": s, "e": e, "text": "\n".join(lines), "kind": "dialogue"} for s, e, lines in out]


def parse(text):
    """List of dicts: s, e (seconds), text, kind ('dialogue'|'song'|'sign')."""
    text = _normalised(text)
    if is_ass(text):
        return [event.line() for _, event in _ass_lines(text) if event is not None]
    return _cues(text)


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


_TAG_KINDS = (("{", "}"), ("<", ">"))   # (opener, closer): an ASS override block, an HTML tag
_TAGS = {False: re.compile(r"\{[^}]*\}"), True: re.compile(r"\{[^}]*\}|<[^>]*>")}   # by strip_tags' html


def strip_tags(text, repl="", html=True):
    """text with each ASS override block ({...}) and, with html, each HTML tag (<...>) replaced by repl: from an opener
    to the first closer of its kind after it, left to right, as re.sub(r"\\{[^}]*\\}|<[^>]*>", repl, text) does. That
    pattern takes quadratic time over openers with no closer after them (it reads on to the end of the text from each
    one), so a text that has any is walked from tag to tag instead, in linear time."""
    kinds = _TAG_KINDS if html else _TAG_KINDS[:1]
    last = {o: text.rfind(c) for o, c in kinds}   # an opener after the last closer of its kind starts no tag
    if all(text.rfind(o) <= last[o] for o, _ in kinds):
        return _TAGS[html].sub(repl, text)
    closer = dict(kinds)
    nxt = {o: text.find(o, 0, max(last[o], 0)) for o in closer}   # the next opener of each kind that starts a tag
    out, done = [], 0
    while True:
        starts = [i for i in nxt.values() if i >= 0]
        if not starts:
            break
        i = min(starts)
        out += (text[done:i], repl)
        done = text.find(closer[text[i]], i + 1) + 1
        for o in closer:
            if 0 <= nxt[o] < done:
                nxt[o] = text.find(o, done, max(last[o], 0))
    out.append(text[done:])
    return "".join(out)


# an ASS vector drawing: its commands are letters, not words ("{" left out of the block's class keeps the search linear)
DRAWING = re.compile(r"\{[^{}]*\\p[1-9]")
_STYLE_TAG = re.compile(r"([ibu])(\d{0,4})")   # ASS italics, bold, underline: 1 on, 0 off; bold also by weight
_POSITION_TAG = re.compile(r"an[1-9]")         # ASS's position mark, which players honour in SRT too
# a WebVTT tag: its name, then, after a dot, space or slash, any classes and annotation (the name ends where the rest
# begins, so an opener with no closer is given up in linear time, not after trying every split of its run)
_VTT_TAG = re.compile(r"<(/?)([^<>\s./]*)(?:[\s./][^<>]*)?>")
_SRT_STYLES = ("i", "b", "u")                  # the styles SRT writes as tags (<i>, <b>, <u>)


def _no_empty_line(text):
    """text without its blank lines (in an SRT cue one would end it), and without space at its ends."""
    return "\n".join(x for x in text.split("\n") if x.strip()).strip()


def _override_tags(block):
    """An ASS override block's tags (its text between the braces), each without its backslash; the ones an animation
    (\\t(...)) holds are left out."""
    tags, depth = [], 0
    for part in block.split("\\")[1:]:   # text before the first backslash is a comment
        if not depth:
            tags.append(part.strip())
        depth = max(0, depth + part.count("(") - part.count(")"))
    return tags


def _ass_srt_text(text):
    """An ASS line's text as SRT writes it: italics, bold and underline as <i>, <b> and <u>, the position mark
    {\\anN} kept, every other override block out (an opener with no closer after it is text, as strip_tags reads it);
    line breaks (\\N, \\n) as new lines and hard spaces (\\h) as spaces."""
    out, styles, at = [], [], 0     # styles: the ones open, in the order they opened
    while True:
        i = text.find("{", at)
        j = text.find("}", i + 1) if i >= 0 else -1
        out.append(text[at:i if j >= 0 else None].replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " "))
        if j < 0:
            break
        for tag in _override_tags(text[i + 1:j]):
            style = _STYLE_TAG.fullmatch(tag)
            if style:
                name, n = style[1], int(style[2] or 0)
                on = n == 1 or (name == "b" and n > 400)
                if on and name not in styles:
                    out.append(f"<{name}>")
                    styles.append(name)
                elif not on and name in styles:
                    out.append(f"</{name}>")
                    styles.remove(name)
            elif _POSITION_TAG.fullmatch(tag):
                out.append(f"{{\\{tag}}}")
            elif tag.startswith("r"):   # back to the line's style: what was turned on in the line is off
                out += [f"</{s}>" for s in reversed(styles)]
                styles.clear()
        at = j + 1
    out += [f"</{s}>" for s in reversed(styles)]
    return _no_empty_line("".join(out))


def _vtt_srt_text(text):
    """A WebVTT cue's text as SRT writes it: <i>, <b> and <u> kept (without their classes), every other tag
    out (voice, class, language, ruby, in-line times), and character references such as &amp; as their characters."""
    def tag(m):
        name = m[2].lower()
        return f"<{m[1]}{name}>" if name in _SRT_STYLES else ""
    return _no_empty_line(unescape(_VTT_TAG.sub(tag, text)))


# a line's text as to_srt writes it, by the format it was read from: an SRT's as it is
_SRT_TEXT = {"srt": lambda text: text, "vtt": _vtt_srt_text, "ass": _ass_srt_text}


def format_of(text):
    """"ass", "vtt" or "srt": the format of a subtitle's text."""
    if is_ass(text):
        return "ass"
    return "vtt" if text.lstrip("\ufeff").startswith("WEBVTT") else "srt"


def to_srt(lines, fmt="srt"):
    """lines (parse()'s, of a subtitle in format `fmt`: format_of) as SRT: one writer for every source format
    (_SRT_TEXT), so a copy differs from its original only in timing. Lines marked drop are left out, and so are ASS
    drawings."""
    srt_text, out = _SRT_TEXT[fmt], []
    for l in lines:
        if l.get("drop") or (fmt == "ass" and DRAWING.search(l["text"])):
            continue
        out.append(f"{len(out) + 1}\n{ts(l['s'])} --> {ts(l['e'])}\n{srt_text(l['text'])}\n")
    return "\n".join(out)


def write_like(src_text, lines):
    """src_text re-timed with lines (same count and order as parse(src_text)); ASS keeps every other field,
    the header and the styles; SRT/WebVTT come back as SRT (to_srt). Lines marked drop are left out."""
    text = _normalised(src_text)
    if not is_ass(text):
        return to_srt(lines, format_of(text))
    it = iter(lines)
    out = []
    for line, event in _ass_lines(text):
        if event is None:
            out.append(line)
            continue
        l = next(it)
        if l.get("drop"):
            continue
        parts = list(event.parts)
        parts[event.start_at], parts[event.end_at] = ass_ts(l["s"]), ass_ts(l["e"])
        out.append("Dialogue:" + ",".join(parts))
    return "\n".join(out)


def plain_srt(lines):
    """Every line, in file order, as a plain SRT block with placeholder text: the candidate as alass gets it
    (alass refuses WebVTT and SRT with a malformed index line; parse() reads both)."""
    out = []
    for i, l in enumerate(lines, 1):
        out.append(f"{i}\n{ts(l['s'])} --> {ts(max(l['e'], l['s'] + 0.001))}\nx\n")
    return "\n".join(out)


# the shortest a reference cue lasts in timing_srt: a cue whose block gives no duration (or a shorter one) still covers
# a line's start for alass, which aligns by overlap
MIN_REFERENCE_CUE_S = 0.5


def timing_srt(starts_durations):
    """An SRT of timings only (the reference made from Matroska Cues)."""
    out = []
    for i, (s, d) in enumerate(sorted(starts_durations), 1):
        out.append(f"{i}\n{ts(s)} --> {ts(s + max(d, MIN_REFERENCE_CUE_S))}\nx\n")
    return "\n".join(out)


# a credit or advertising line a site or an uploader adds at the start or the end of a subtitle
CREDIT = re.compile(r"(?i)https?://|www\.|\.(com|org|net|info|tv|ru)\b|subtitles? (by|from)|downloaded from|"
                    r"sync(ed|hronized)? (and corrected )?by|corrected by|ripped by|translated by|opensubtitles|"
                    r"subdl|subsource|addic7ed|advertise")


def plain_line(text):
    """A line's words as the language check and fingerprints compare them: markup (ASS overrides, HTML tags) and ASS
    line breaks and hard spaces gone, spaces collapsed, lower case."""
    text = strip_tags(text or "").replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return " ".join(text.split()).lower()


WEBVTT_NOTE = re.compile(r"(?im)^NOTE\b[ \t]*")


def fingerprint(text):
    """What makes two subtitles the same one: their dialogue lines' words and start times to a tenth of a
    second, once credit lines at the start and the end (at most srclimits.FINGERPRINT_CREDIT_LINES each) are left out,
    and for a subtitle of more than srclimits.FINGERPRINT_LONGER_THAN lines its first and last
    srclimits.FINGERPRINT_EDGE lines too: OpenSubtitles V3+ adds a notice line first and marks the first cue's lines
    with WebVTT's "NOTE". Another encoding, line ending, byte-order mark, format (SRT or WebVTT) or notice
    doesn't change it; another timing or text does."""
    lines = [l for l in parse(text) if l["kind"] == "dialogue"]
    for end in (0, -1):   # the first lines, then the last
        for _ in range(srclimits.FINGERPRINT_CREDIT_LINES):
            if not lines or not CREDIT.search(plain_line(lines[end]["text"])):
                break
            lines.pop(end)
    if len(lines) > srclimits.FINGERPRINT_LONGER_THAN:
        lines = lines[srclimits.FINGERPRINT_EDGE:len(lines) - srclimits.FINGERPRINT_EDGE]
    body = "\n".join(f"{round(l['s'], 1):.1f}|{plain_line(WEBVTT_NOTE.sub('', l['text']))}" for l in lines)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


NO_DIALOGUE = fingerprint("")   # the fingerprint of every text with no dialogue line (an error page, a broken download)


def _srt_text(text, fmt):
    """A line's text, of a subtitle in format `fmt`, as to_srt writes it and parse reads it back (for matching a copy's
    lines to its original's)."""
    return _SRT_TEXT[fmt](text).strip()


def _keyed_lines(text):
    """parse(text)'s lines, each with what a re-timing leaves as it is: of an ASS line every field but Start and End,
    of an SRT or WebVTT line its text as to_srt writes it."""
    text = _normalised(text)
    if is_ass(text):
        return [(event.line(), event.kept()) for _, event in _ass_lines(text) if event is not None]
    fmt = format_of(text)
    return [(l, _srt_text(l["text"], fmt)) for l in parse(text)]


# the form of the recipes timing_recipe writes ("v"): apply_recipe refuses another, such as one a later version kept
# (a rollback runs this version on the data as it is), instead of reading it as this one
RECIPE_VERSION = 1


def timing_recipe(original, body):
    """How `body` was made from `original`: each original line's new start and end in milliseconds, or None
    for a line left out; {"same": True} when the copy is the original as it is. No text. None when `body` isn't a
    re-timing of `original` that apply_recipe rebuilds exactly."""
    if body == original:
        return {"v": RECIPE_VERSION, "same": True}
    src, out = _keyed_lines(original), _keyed_lines(body)
    times, j = [], 0
    for _, kept in src:
        if j < len(out) and out[j][1] == kept:
            line = out[j][0]
            times.append([int(round(line["s"] * 1000)), int(round(line["e"] * 1000))])
            j += 1
        else:
            times.append(None)
    if j != len(out):
        return None
    recipe = {"v": RECIPE_VERSION, "times": times}
    return recipe if apply_recipe(original, recipe) == body else None


def apply_recipe(original, recipe):
    """The copy a timing recipe makes from its original, or None when the original doesn't fit it (another number
    of lines) or the recipe is of another form (RECIPE_VERSION)."""
    if not isinstance(recipe, dict) or recipe.get("v") != RECIPE_VERSION:
        return None
    if recipe.get("same"):
        return original
    times = recipe.get("times")
    src = parse(original)
    if not isinstance(times, list) or len(times) != len(src):
        return None
    lines = [dict(l, s=t[0] / 1000, e=t[1] / 1000) if t else dict(l, drop=True) for l, t in zip(src, times)]
    return write_like(original, lines)


def line_positions(text):
    """Where a subtitle's lines are, without their words: a hash of every line's start and end in
    milliseconds, in file order. A recipe gives each line of its original a time by its place, so an original whose
    lines moved (a credit line moved from the end to the start) would give each line its neighbour's time."""
    times = ";".join(f"{round(l['s'] * 1000)},{round(l['e'] * 1000)}" for l in parse(text))
    return hashlib.sha256(times.encode("ascii")).hexdigest()


def recognised(text, recipe):
    """Is `text`, the original a recipe was made from but with other bytes now, still that subtitle: the same
    words and timings (fingerprint, recipe["fp"]) and its lines where they were (line_positions, recipe["positions"])?
    A recipe kept without the positions (before) recognises no changed original, only the same bytes."""
    return (isinstance(recipe, dict) and recipe.get("positions") is not None
            and recipe.get("fp") == fingerprint(text) and recipe["positions"] == line_positions(text))
