"""Which subtitles to try for a file, and in what order.

The viewer's first subtitle is always tried first: when it is in sync there is nothing to do. After it, subtitles made
for this very release are the likeliest to be in sync already, so they go next:
  1. OpenSubtitles hash match (`moviehash` equal to the file's hash);
  2. the file's release group named in the subtitle's release (`[RedDeadProject] Spirited Away.mkv` and
     `Spirited Away (2001) JAPANESE.Bluray.REMUX.H264.6.1[RedDeadProject]`);
  3. the same kind of source (remux, Blu-ray, web, TV, DVD);
  4. everything else, in the viewer's order.
Machine-translated subtitles go last. Ties keep the viewer's order, which is also the rank the store records.
"""
import re

TAGS = {
    "remux": ("remux",),
    "bluray": ("bluray", "blu-ray", "bdrip", "brrip", "bdremux", "bd", "bdmv", "bd1080p", "bd720p"),
    "web": ("web", "web-dl", "webdl", "webrip", "nf", "amzn", "dsnp", "hmax", "atvp", "cr"),
    "tv": ("hdtv", "hdtvrip", "tvrip", "pdtv"),
    "dvd": ("dvd", "dvdrip", "dvd5", "dvd9", "r5"),
}
NOT_GROUPS = {"multiple subtitle", "multi-subs", "multi subs", "dual audio", "dual-audio", "eng", "jpn", "jap", "rus",
              "english", "japanese", "russian", "subbed", "dubbed", "uncensored", "batch", "complete", "proper",
              "repack", "hevc", "x264", "x265", "h264", "h265", "avc", "aac", "flac", "ac3", "dts", "10bit", "8bit",
              "hdr", "dv", "sdr", "1080p", "720p", "2160p", "480p", "576p", "4k", "uhd"}
HEX = re.compile(r"^[0-9a-f]{8}$", re.I)
WORD = re.compile(r"[a-z0-9]+")


def _norm(text):
    return " ".join(WORD.findall((text or "").lower()))


def groups(filename):
    """Release groups a file name names: bracketed names (fansub style) and a trailing `-GROUP`."""
    name = re.sub(r"\.(mkv|mp4|avi|m2ts|ts)$", "", filename or "", flags=re.I)
    out = set()
    for g in re.findall(r"\[([^\]]+)\]", name):
        g = g.strip()
        low = g.lower()
        if HEX.match(g) or low in NOT_GROUPS or any(low in v for v in TAGS.values()) or not re.search(r"[a-z]", low):
            continue
        if re.search(r"\b(1080p|720p|2160p|480p|bd|dvd|web|hevc|x26[45]|flac|aac)\b", low):
            continue   # a tag list like "BD 1080p HEVC"
        out.add(_norm(g))
    m = re.search(r"-([A-Za-z0-9][A-Za-z0-9_.]{1,30})$", re.sub(r"\[[^\]]*\]|\([^)]*\)", "", name).strip())
    if m and m.group(1).lower() not in NOT_GROUPS:
        out.add(_norm(m.group(1)))
    return {g for g in out if len(g) >= 2}


def source_kind(text):
    words = set(re.findall(r"[a-z0-9-]+", (text or "").lower().replace(".", " ").replace("_", " ")))
    for kind in ("remux", "bluray", "web", "tv", "dvd"):
        if words & set(TAGS[kind]):
            return kind
    return None


def order(candidates, filename=None, file_hash=None):
    """candidates: in the viewer's order, each with optional `release`, `hash`, `hash_match`, `ai`. Returns [(viewer rank, cand)]
    in the order to try them."""
    gs = groups(filename)
    kind = source_kind(filename)
    fh = (file_hash or "").lower()

    def tier(rank, c):
        if rank == 0:
            return 0
        if c.get("ai"):
            return 9
        rel = " " + _norm(c.get("release")) + " "
        if (fh and c.get("hash") == fh) or c.get("hash_match"):   # hash_match: the addon matched this file's hash
            return 1
        if gs and any(f" {g} " in rel for g in gs):
            return 2
        if kind and source_kind(c.get("release")) == kind:
            return 3
        return 4

    ranked = list(enumerate(candidates))
    return sorted(ranked, key=lambda rc: (tier(*rc), rc[0]))
