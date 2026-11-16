"""Language codes: everything to ISO 639-2/B (what Matroska and most players use), plus display names. The two-letter
table is all of ISO 639-1 and the terminologic spellings all of ISO 639-2's twenty (from the Library of Congress's ISO
639-2 list); the names are the list's first English name, except the 32 names the service always used."""

TWO = {"aa": "aar", "ab": "abk", "ae": "ave", "af": "afr", "ak": "aka", "am": "amh", "an": "arg", "ar": "ara",
       "as": "asm", "av": "ava", "ay": "aym", "az": "aze", "ba": "bak", "be": "bel", "bg": "bul", "bi": "bis",
       "bm": "bam", "bn": "ben", "bo": "tib", "br": "bre", "bs": "bos", "ca": "cat", "ce": "che", "ch": "cha",
       "co": "cos", "cr": "cre", "cs": "cze", "cu": "chu", "cv": "chv", "cy": "wel", "da": "dan", "de": "ger",
       "dv": "div", "dz": "dzo", "ee": "ewe", "el": "gre", "en": "eng", "eo": "epo", "es": "spa", "et": "est",
       "eu": "baq", "fa": "per", "ff": "ful", "fi": "fin", "fj": "fij", "fo": "fao", "fr": "fre", "fy": "fry",
       "ga": "gle", "gd": "gla", "gl": "glg", "gn": "grn", "gu": "guj", "gv": "glv", "ha": "hau", "he": "heb",
       "hi": "hin", "ho": "hmo", "hr": "hrv", "ht": "hat", "hu": "hun", "hy": "arm", "hz": "her", "ia": "ina",
       "id": "ind", "ie": "ile", "ig": "ibo", "ii": "iii", "ik": "ipk", "io": "ido", "is": "ice", "it": "ita",
       "iu": "iku", "ja": "jpn", "jv": "jav", "ka": "geo", "kg": "kon", "ki": "kik", "kj": "kua", "kk": "kaz",
       "kl": "kal", "km": "khm", "kn": "kan", "ko": "kor", "kr": "kau", "ks": "kas", "ku": "kur", "kv": "kom",
       "kw": "cor", "ky": "kir", "la": "lat", "lb": "ltz", "lg": "lug", "li": "lim", "ln": "lin", "lo": "lao",
       "lt": "lit", "lu": "lub", "lv": "lav", "mg": "mlg", "mh": "mah", "mi": "mao", "mk": "mac", "ml": "mal",
       "mn": "mon", "mr": "mar", "ms": "may", "mt": "mlt", "my": "bur", "na": "nau", "nb": "nob", "nd": "nde",
       "ne": "nep", "ng": "ndo", "nl": "dut", "nn": "nno", "no": "nor", "nr": "nbl", "nv": "nav", "ny": "nya",
       "oc": "oci", "oj": "oji", "om": "orm", "or": "ori", "os": "oss", "pa": "pan", "pi": "pli", "pl": "pol",
       "ps": "pus", "pt": "por", "qu": "que", "rm": "roh", "rn": "run", "ro": "rum", "ru": "rus", "rw": "kin",
       "sa": "san", "sc": "srd", "sd": "snd", "se": "sme", "sg": "sag", "si": "sin", "sk": "slo", "sl": "slv",
       "sm": "smo", "sn": "sna", "so": "som", "sq": "alb", "sr": "srp", "ss": "ssw", "st": "sot", "su": "sun",
       "sv": "swe", "sw": "swa", "ta": "tam", "te": "tel", "tg": "tgk", "th": "tha", "ti": "tir", "tk": "tuk",
       "tl": "tgl", "tn": "tsn", "to": "ton", "tr": "tur", "ts": "tso", "tt": "tat", "tw": "twi", "ty": "tah",
       "ug": "uig", "uk": "ukr", "ur": "urd", "uz": "uzb", "ve": "ven", "vi": "vie", "vo": "vol", "wa": "wln",
       "wo": "wol", "xh": "xho", "yi": "yid", "yo": "yor", "za": "zha", "zh": "chi", "zu": "zul"}
TERMINOLOGIC = {"bod": "tib", "ces": "cze", "cym": "wel", "deu": "ger", "ell": "gre", "eus": "baq", "fas": "per",
                "fra": "fre", "hye": "arm", "isl": "ice", "kat": "geo", "mkd": "mac", "mri": "mao", "msa": "may",
                "mya": "bur", "nld": "dut", "pob": "por", "ron": "rum", "slk": "slo", "sqi": "alb", "zho": "chi"}
NAMES = {"aar": "Afar", "abk": "Abkhazian", "afr": "Afrikaans", "aka": "Akan", "alb": "Albanian", "amh": "Amharic",
         "ara": "Arabic", "arg": "Aragonese", "arm": "Armenian", "asm": "Assamese", "ava": "Avaric", "ave": "Avestan",
         "aym": "Aymara", "aze": "Azerbaijani", "bak": "Bashkir", "bam": "Bambara", "baq": "Basque",
         "bel": "Belarusian", "ben": "Bengali", "bis": "Bislama", "bos": "Bosnian", "bre": "Breton",
         "bul": "Bulgarian", "bur": "Burmese", "cat": "Catalan", "cha": "Chamorro", "che": "Chechen", "chi": "Chinese",
         "chu": "Church Slavic", "chv": "Chuvash", "cor": "Cornish", "cos": "Corsican", "cre": "Cree", "cze": "Czech",
         "dan": "Danish", "div": "Divehi", "dut": "Dutch", "dzo": "Dzongkha", "eng": "English", "epo": "Esperanto",
         "est": "Estonian", "ewe": "Ewe", "fao": "Faroese", "fij": "Fijian", "fin": "Finnish", "fre": "French",
         "fry": "Western Frisian", "ful": "Fulah", "geo": "Georgian", "ger": "German", "gla": "Gaelic", "gle": "Irish",
         "glg": "Galician", "glv": "Manx", "gre": "Greek", "grn": "Guarani", "guj": "Gujarati", "hat": "Haitian",
         "hau": "Hausa", "heb": "Hebrew", "her": "Herero", "hin": "Hindi", "hmo": "Hiri Motu", "hrv": "Croatian",
         "hun": "Hungarian", "ibo": "Igbo", "ice": "Icelandic", "ido": "Ido", "iii": "Sichuan Yi", "iku": "Inuktitut",
         "ile": "Interlingue", "ina": "Interlingua", "ind": "Indonesian", "ipk": "Inupiaq", "ita": "Italian",
         "jav": "Javanese", "jpn": "Japanese", "kal": "Kalaallisut", "kan": "Kannada", "kas": "Kashmiri",
         "kau": "Kanuri", "kaz": "Kazakh", "khm": "Central Khmer", "kik": "Kikuyu", "kin": "Kinyarwanda",
         "kir": "Kirghiz", "kom": "Komi", "kon": "Kongo", "kor": "Korean", "kua": "Kuanyama", "kur": "Kurdish",
         "lao": "Lao", "lat": "Latin", "lav": "Latvian", "lim": "Limburgan", "lin": "Lingala", "lit": "Lithuanian",
         "ltz": "Luxembourgish", "lub": "Luba-Katanga", "lug": "Ganda", "mac": "Macedonian", "mah": "Marshallese",
         "mal": "Malayalam", "mao": "Maori", "mar": "Marathi", "may": "Malay", "mlg": "Malagasy", "mlt": "Maltese",
         "mon": "Mongolian", "nau": "Nauru", "nav": "Navajo", "nbl": "South Ndebele", "nde": "North Ndebele",
         "ndo": "Ndonga", "nep": "Nepali", "nno": "Norwegian Nynorsk", "nob": "Norwegian Bokmål", "nor": "Norwegian",
         "nya": "Chichewa", "oci": "Occitan", "oji": "Ojibwa", "ori": "Oriya", "orm": "Oromo", "oss": "Ossetian",
         "pan": "Panjabi", "per": "Persian", "pli": "Pali", "pol": "Polish", "por": "Portuguese", "pus": "Pushto",
         "que": "Quechua", "roh": "Romansh", "rum": "Romanian", "run": "Rundi", "rus": "Russian", "sag": "Sango",
         "san": "Sanskrit", "sin": "Sinhala", "slo": "Slovak", "slv": "Slovenian", "sme": "Northern Sami",
         "smo": "Samoan", "sna": "Shona", "snd": "Sindhi", "som": "Somali", "sot": "Sotho, Southern", "spa": "Spanish",
         "srd": "Sardinian", "srp": "Serbian", "ssw": "Swati", "sun": "Sundanese", "swa": "Swahili", "swe": "Swedish",
         "tah": "Tahitian", "tam": "Tamil", "tat": "Tatar", "tel": "Telugu", "tgk": "Tajik", "tgl": "Tagalog",
         "tha": "Thai", "tib": "Tibetan", "tir": "Tigrinya", "ton": "Tonga", "tsn": "Tswana", "tso": "Tsonga",
         "tuk": "Turkmen", "tur": "Turkish", "twi": "Twi", "uig": "Uighur", "ukr": "Ukrainian", "urd": "Urdu",
         "uzb": "Uzbek", "ven": "Venda", "vie": "Vietnamese", "vol": "Volapük", "wel": "Welsh", "wln": "Walloon",
         "wol": "Wolof", "xho": "Xhosa", "yid": "Yiddish", "yor": "Yoruba", "zha": "Zhuang", "zul": "Zulu"}
BY_NAME = {v.lower(): k for k, v in NAMES.items()}


# TMDB's original languages are ISO 639-1 codes, plus two of its own: "cn" (Cantonese, which Matroska tracks tag as
# Chinese) and "xx" (no language)
TMDB_CODES = {"cn": "chi", "xx": None}
# codes that name no single language: undetermined, several, no speech, not coded; qaa-qtz are for local use
NOT_A_LANGUAGE = {"und", "mul", "zxx", "mis"}
# languages counted as one when a title's original language is compared with a file's tracks (only there): the Chinese
# varieties, Norwegian's two written forms, and the standard forms of Arabic, Persian and Malay
FAMILY = {"yue": "chi", "cmn": "chi", "nan": "chi", "hak": "chi", "wuu": "chi", "nob": "nor", "nno": "nor",
          "arb": "ara", "pes": "per", "prs": "per", "zsm": "may"}


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


def known(code):
    """A code (two or three letters, an IETF tag or an English name) as a three-letter language code; None when it
    names no language."""
    c = lang3(code)
    if len(c) != 3 or not c.isalpha() or not c.islower() or c in NOT_A_LANGUAGE or "qaa" <= c <= "qtz":
        return None
    return c


def from_tmdb(code):
    """TMDB's original_language as a three-letter code; None for "no language" or a code outside ISO 639-1."""
    c = str(code or "").strip().lower()
    if c in TMDB_CODES:
        return TMDB_CODES[c]
    return TWO.get(c)


def same_language(a, b):
    """Whether two codes name the same language for the original-language comparison (FAMILY's members count as one)."""
    x, y = known(a), known(b)
    return x is not None and y is not None and FAMILY.get(x, x) == FAMILY.get(y, y)
