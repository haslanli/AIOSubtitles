"""Is a subtitle's text in the language it is listed under? Sites and addons sometimes list a subtitle under
another language than its text's (Friends S01E01's "English" original was Spanish), and such a subtitle can pass on its
timing alone. The check reads the text with its markup removed (SRT, WebVTT, ASS: tags, drawings, songs and signs left
out) and measures two things:

  the script     the share of its writing in the language's script (Latin, Cyrillic, Greek, Arabic, Hebrew, Han,
                 Kana, Hangul, Thai, Devanagari), each Han, kana or Hangul character one unit against one word of
                 another script (a bilingual Chinese/English subtitle is Chinese);
  common words   the share of its words that are the language's most common function words, against the other
                 languages the check knows (only words found in one list count, so "no" or "de" tell nothing).

No verdict under MIN_WORDS words, so a short subtitle is never refused; a language without a word list gets the script
check only, and one without a known script neither. Refused only with a clear margin: the expected script under half the
units, or the expected language's common words under 3 % of the words and below another known language's. It never
re-labels a subtitle, it only refuses it. No packages besides the standard library.
"""
import bisect
import re
import unicodedata

from .langs import lang3
from .subs import DRAWING, parse, plain_line

PASS, FAIL, NO_VERDICT = "pass", "fail", "no verdict"

MIN_WORDS = 300            # fewer words: no verdict
MIN_SCRIPT_SHARE = 0.5     # refused when less than this share of the units (measure) is in the language's script
MIN_COMMON_SHARE = 0.03    # ... or its common words are less than this share of the words and below another language's
MORE_DOWNLOADS = 3         # an original refused when served: at most this many more of the file's subtitles downloaded

# Unicode blocks of each script: (first code point, last code point, script)
_BLOCKS = sorted([
    (0x41, 0x5A, "latin"), (0x61, 0x7A, "latin"), (0xAA, 0xAA, "latin"), (0xBA, 0xBA, "latin"),
    (0xC0, 0x2AF, "latin"), (0x1D00, 0x1DBF, "latin"), (0x1E00, 0x1EFF, "latin"), (0x2C60, 0x2C7F, "latin"),
    (0xA720, 0xA7FF, "latin"), (0xFF21, 0xFF3A, "latin"), (0xFF41, 0xFF5A, "latin"),
    (0x370, 0x3FF, "greek"), (0x1F00, 0x1FFF, "greek"),
    (0x400, 0x52F, "cyrillic"), (0x1C80, 0x1C8F, "cyrillic"), (0x2DE0, 0x2DFF, "cyrillic"), (0xA640, 0xA69F, "cyrillic"),
    (0x590, 0x5FF, "hebrew"), (0xFB1D, 0xFB4F, "hebrew"),
    (0x600, 0x6FF, "arabic"), (0x750, 0x77F, "arabic"), (0x8A0, 0x8FF, "arabic"), (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFF, "arabic"),
    (0x900, 0x97F, "devanagari"), (0xA8E0, 0xA8FF, "devanagari"),
    (0xE00, 0xE7F, "thai"),
    (0x1100, 0x11FF, "hangul"), (0x3130, 0x318F, "hangul"), (0xA960, 0xA97F, "hangul"), (0xAC00, 0xD7AF, "hangul"),
    (0xD7B0, 0xD7FF, "hangul"), (0xFFA0, 0xFFDC, "hangul"),
    (0x3040, 0x30FF, "kana"), (0x31F0, 0x31FF, "kana"), (0xFF66, 0xFF9F, "kana"),
    (0x3400, 0x4DBF, "han"), (0x4E00, 0x9FFF, "han"), (0xF900, 0xFAFF, "han"), (0x20000, 0x323AF, "han"),
])
_STARTS = [b[0] for b in _BLOCKS]
# scripts written a syllable or a word to a character: each character one unit of the script share, as a word of any
# other script is
CHARACTER_SCRIPTS = ("han", "kana", "hangul")

# The scripts a language is written in, by its 3-letter code (langs.lang3)
_LATIN = ("eng spa fre ger ita por dut swe nor dan fin cze slo hun rum hrv bos slv pol tur ind may vie est lav lit cat "
          "glg baq ice alb tgl")
SCRIPTS = {code: ("latin",) for code in _LATIN.split()}
SCRIPTS.update({code: ("cyrillic",) for code in "rus ukr bul bel mac kaz".split()})
SCRIPTS.update({
    "srp": ("cyrillic", "latin"),   # Serbian is written in both
    "gre": ("greek",), "ara": ("arabic",), "per": ("arabic",), "urd": ("arabic",), "heb": ("hebrew",),
    "hin": ("devanagari",), "mar": ("devanagari",), "nep": ("devanagari",), "tha": ("thai",),
    "chi": ("han",), "jpn": ("han", "kana"), "kor": ("hangul", "han"),
})

# The most common function words of subtitle dialogue, lower case
WORDS = {code: frozenset(text.split()) for code, text in {
    "eng": "the you i to and it of that is what we me this he for my on have your do was be not are just with but so "
           "all know get like can they there she her him go here out up about right if at one come now how well want "
           "think will when who why yeah okay oh",
    "spa": "de que no a la el es y en lo un por qué me una te los se con para mi está si bien pero yo eso las sí su "
           "tu aquí del al como le más esto ya todo esta muy hay estoy cuando él nada ser sé tengo puedo así algo "
           "vamos bueno",
    "fre": "de je est pas le vous la tu que un il et à ne les ce en on ça une ai pour des moi qui nous mais me dans "
           "du bien elle si tout plus non mon suis te va sur comme avec oui fait être faire veux sais quoi rien là",
    "ger": "ich sie das ist du nicht die und es der wir was zu er ein in mir mit ja wie den auf mich dass so hier "
           "eine wenn hat dich noch nur doch an von sind war auch mal haben nein kann bin schon gut für jetzt dir uns "
           "weiß habe",
    "ita": "non di che è e la il un a per mi sono ti ho lo ma si cosa una le me in con no questo bene come mio qui "
           "sei hai del se perché tu sì ci io ha anche fare solo va niente più tutto ancora siamo lei",
    "por": "que não o de a é e eu um você se do da para isso me uma está em com os no te na ele por mas meu ao sim "
           "tem bem aqui estou como foi vai só ela nós são seu tudo já sei muito agora",
    "dut": "ik je het de is dat een niet en wat van we in ze hij op te zijn er maar met die voor me heb dit hier wel "
           "weet als ben mijn naar jij nu kan zo om ook nog moet hebben dan gaan geen jullie",
    "vie": "không tôi là của và anh có một em này được cho người đi đó với cái gì những như bạn sẽ thì đã ông làm "
           "phải chúng nó ở biết nhưng ta rồi lại khi cô vậy đây chỉ mà nào hãy nói muốn",
}.items()}
# a word in two lists tells the languages apart no better than a name: only words of one list count
_DISTINCT = {code: frozenset(w for w in words if not any(w in other for c, other in WORDS.items() if c != code))
             for code, words in WORDS.items()}

_TOKEN = re.compile(r"[^\W\d_]+")


def script_of(ch):
    """The script of one letter, or None for one of another script."""
    cp = ord(ch)
    i = bisect.bisect_right(_STARTS, cp) - 1
    if i >= 0 and _BLOCKS[i][0] <= cp <= _BLOCKS[i][1]:
        return _BLOCKS[i][2]
    return None


def plain_text(text):
    """A subtitle's dialogue with its markup removed, one line of text per subtitle line, lower case."""
    lines = []
    for line in parse(text or ""):
        if line["kind"] != "dialogue" or DRAWING.search(line["text"]):   # a drawing's commands are letters, not words
            continue
        lines.append(plain_line(line["text"]))
    return unicodedata.normalize("NFC", "\n".join(lines))


def units(plain):
    """The scripts of a text's units of writing, in order: each Han, kana or Hangul character one (CHARACTER_SCRIPTS),
    each run of letters of one other script (a word) one; a combining mark belongs to its letter's run."""
    out, run = [], None   # run: the script of the word being read, None between words
    for ch in plain:
        if not ch.isalpha():
            if not unicodedata.category(ch).startswith("M"):
                run = None
            continue
        script = script_of(ch) or "other"
        if script in CHARACTER_SCRIPTS:
            out.append(script)
            run = None
        elif script != run:
            out.append(script)
            run = script
    return out


def measure(text, lang):
    """What the check reads: {"words", "letters", "script" (share of the units in the language's scripts, None when
    its script isn't known), "common" ({code: share of the words that are that language's common words})}."""
    code = lang3(lang)
    plain = plain_text(text)
    words = _TOKEN.findall(plain)
    letters = [ch for ch in plain if ch.isalpha()]
    scripts = SCRIPTS.get(code)
    script = None
    written = units(plain) if scripts else []
    if written:
        script = round(sum(1 for s in written if s in scripts) / len(written), 3)
    common = {c: round(sum(1 for w in words if w in distinct) / len(words), 4) if words else 0.0
              for c, distinct in _DISTINCT.items()}
    return {"words": len(words), "letters": len(letters), "script": script, "common": common}


def verdict(m, lang):
    """PASS, FAIL or NO_VERDICT for a measure (measure())."""
    code = lang3(lang)
    if m["words"] < MIN_WORDS:
        return NO_VERDICT
    known = False
    if m["script"] is not None:
        known = True
        if m["script"] < MIN_SCRIPT_SHARE:
            return FAIL
    if code in WORDS:
        known = True
        own = m["common"][code]
        if own < MIN_COMMON_SHARE and any(share > own for c, share in m["common"].items() if c != code):
            return FAIL
    return PASS if known else NO_VERDICT


def check(text, lang):
    """PASS, FAIL or NO_VERDICT: is `text` (a subtitle, any format subs.parse reads) in language `lang`?"""
    return verdict(measure(text, lang), lang)


def refuses(text, lang):
    """True when the check refuses `text` as language `lang` (no verdict is never a refusal)."""
    return check(text, lang) == FAIL
