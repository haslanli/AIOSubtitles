"""Which subtitles to try for a file, and in what order.

The viewer's first subtitle is always tried first: when it is in sync there is nothing to do. After it, subtitles made
for this very release are the likeliest to be in sync already, so they go next:
  1. OpenSubtitles hash match (`moviehash` equal to the file's hash);
  2. the file's release group named in the subtitle's release (`[RedDeadProject] Spirited Away.mkv` and
     `Spirited Away (2001) JAPANESE.Bluray.REMUX.H264.6.1[RedDeadProject]`);
  3. the same kind of source (remux, Blu-ray, web, TV, DVD);
  4. everything else, in the viewer's order.
Machine-translated subtitles go last; with `skip` AI-made, machine-translated and forced subtitles are left
out altogether, the viewer's first too. Ties keep the viewer's order, which is also the rank the store records.
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
UNWANTED = re.compile(r"(?<![a-z])(ai[ ._-]?(translated|made|generated)|machine[ ._-]?translat\w*|auto[ ._-]?translat\w*|"
                      r"google[ ._-]?translat\w*|deepl|forced|foreign[ ._-]?parts?([ ._-]?only)?)(?![a-z])", re.I)


def unwanted(c):
    """AI-made, machine-translated or forced (flagged by the source, or named so in its release)."""
    return bool(c.get("ai")) or bool(UNWANTED.search(c.get("release") or ""))


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


def _tier(rank, c, gs, kind, fh, ref_cues, viewer_first):
    if rank == 0 and viewer_first:
        return 0
    if c.get("ai"):
        return 9
    rel = " " + _norm(c.get("release")) + " "
    if (fh and c.get("hash") == fh) or c.get("hash_match"):   # hash_match: the source matched this file's hash
        return 1
    if ref_cues and c.get("cues") and abs(c["cues"] - ref_cues) <= 0.02 * ref_cues:
        return 1   # as many lines as the release's own track: most likely made from this release
    if c.get("release_match") or (gs and any(f" {g} " in rel for g in gs)):
        return 2   # found by the file's release name (TheSubtitleDB), or it names the file's release group
    if kind and source_kind(c.get("release")) == kind:
        return 3
    return 4


def tier_of(rank, c, filename=None, file_hash=None, ref_cues=None, viewer_first=True):
    """How likely a candidate is to fit the file as it is: 0 the viewer's first, 1 a hash or line-count match, 2 its
    release (by name or group), 3 the same kind of source, 4 the rest, 9 machine-translated."""
    return _tier(rank, c, groups(filename), source_kind(filename), (file_hash or "").lower(), ref_cues, viewer_first)


def order(candidates, filename=None, file_hash=None, skip=False, ref_cues=None, viewer_first=True):
    """candidates: in the viewer's order, each with optional `release`, `hash`, `hash_match`, `release_match`, `ai`.
    Returns [(viewer rank, cand)] in the order to try them; with viewer_first off the viewer's first has no place of its
    own. Ties keep the list's order: the config's subtitles first, then the built-in sources (srclimits.SOURCE_ORDER)."""
    gs = groups(filename)
    kind = source_kind(filename)
    fh = (file_hash or "").lower()
    ranked = [(r, c) for r, c in enumerate(candidates) if not (skip and unwanted(c))]
    return sorted(ranked, key=lambda rc: (_tier(rc[0], rc[1], gs, kind, fh, ref_cues, viewer_first), rc[0]))


def best_guess(candidates, filename=None, file_hash=None, skip=False):
    """The subtitle likeliest to fit the file as it is while nothing can be checked (a file that waits for the audio
    route, or a job still working): a hash match, then the file's release group, then its kind of source, else
    the viewer's first; with `skip` never an AI-made, machine-translated or forced one. None when there is none."""
    ranked = order(candidates, filename, file_hash, skip=skip, viewer_first=False)
    return ranked[0][1] if ranked else None

