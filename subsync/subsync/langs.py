"""Language codes: everything to ISO 639-2/B (what Matroska and most players use), plus display names."""

TWO = {"en": "eng", "ru": "rus", "ja": "jpn", "fr": "fre", "de": "ger", "es": "spa", "it": "ita", "pt": "por",
       "ko": "kor", "zh": "chi", "uk": "ukr", "pl": "pol", "nl": "dut", "sv": "swe", "tr": "tur", "ar": "ara",
       "he": "heb", "cs": "cze", "hu": "hun", "el": "gre", "fi": "fin", "da": "dan", "no": "nor", "ro": "rum",
       "bg": "bul", "hr": "hrv", "sr": "srp", "id": "ind", "th": "tha", "vi": "vie", "hi": "hin", "fa": "per"}
TERMINOLOGIC = {"deu": "ger", "fra": "fre", "nld": "dut", "ell": "gre", "ron": "rum", "ces": "cze", "zho": "chi",
                "fas": "per", "pob": "por"}
NAMES = {"eng": "English", "rus": "Russian", "jpn": "Japanese", "fre": "French", "ger": "German", "spa": "Spanish",
         "ita": "Italian", "por": "Portuguese", "kor": "Korean", "chi": "Chinese", "ukr": "Ukrainian", "pol": "Polish"}
BY_NAME = {v.lower(): k for k, v in NAMES.items()}


def lang3(code):
    raw = str(code or "").strip()
    key = raw.lower().replace("_", "-").split("-")[0]
    if key in TERMINOLOGIC:
        return TERMINOLOGIC[key]
    if len(key) == 3 and key.isalpha():
        return key
    if key in TWO:
        return TWO[key]
    return BY_NAME.get(raw.lower(), key or "und")


def name(code):
    c = lang3(code)
    return NAMES.get(c, c)
