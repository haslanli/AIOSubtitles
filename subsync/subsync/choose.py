"""Which subtitles to try for a file, and in what order.

The viewer's first subtitle is always tried first: when it is in sync there is nothing to do. After it, subtitles made
for this very release are the likeliest to be in sync already, so they go next:
  1. OpenSubtitles hash match (`moviehash` equal to the file's hash);
  2. the file's release group named in the subtitle's release (`[RedDeadProject] Spirited Away.mkv` and
     `Spirited Away (2001) JAPANESE.Bluray.REMUX.H264.6.1[RedDeadProject]`);
  3. the same kind of source (remux, Blu-ray, web, TV, DVD);
  4. everything else, in the viewer's order.
When the file's name gives an edition (Director's Cut or DC, Extended, Roadshow, Unrated, Theatrical), a subtitle naming
the same one moves up a tier (not above a hash or line-count match) and one naming another goes after everything else
(Kingdom of Heaven DC's theatrical WEB subtitle was the guess by its release group alone).
A release naming another season or episode than the file's loses its release-group and same-source credit (The
Bear S01E01's guess was a season-3 subtitle by its release group alone); one naming several episodes (S01E01E02,
S01E01-E10) or seasons (S01-S03) is not another when the file's is among them. AI-made, machine-translated and forced
subtitles (`unwanted`) go last; with `skip` they are left out altogether, the viewer's first too. Ties keep the
viewer's order, which is also the rank the store records. The season and episode parsing here is also the built-in
sources' (extra.py).
"""
import re
from collections import namedtuple

from .sources import release_name

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
RELEASE_TAGS = NOT_GROUPS | {t for tags in TAGS.values() for t in tags}   # never the first word of a trailing group
HEX = re.compile(r"^[0-9a-f]{8}$", re.I)
WORD = re.compile(r"[a-z0-9]+")
# a bracketed name, and the brackets and parentheses taken out before a trailing -GROUP is looked for: an opener inside
# ends the scan, so a name of many unclosed "[" or "(" is read in linear time, not quadratic
BRACKETED = re.compile(r"\[([^\[\]]+)\]")
BRACKETS = re.compile(r"\[[^\[\]]*\]|\([^()]*\)")
TRAILING_GROUP = re.compile(r"-([A-Za-z0-9][A-Za-z0-9_.]{1,30})$")
# a year and a resolution, as whole words: with an episode tag and RELEASE_TAGS, a release's words, not its title's
YEAR =re.compile(r"(?<![a-z0-9])(?:19|20)[0-9]{2}(?![a-z0-9])", re.I)
RESOLUTION = re.compile(r"(?<![a-z0-9])[0-9]{3,4}p(?![a-z0-9])", re.I)
EDITIONS = {"dc": r"dc|director['’]?s?[ ._-]*cut", "extended": r"extended", "roadshow": r"road[ ._-]?show",
            "unrated": r"unrated", "theatrical": r"theatrical"}
EDITION = {k: re.compile(rf"(?<![a-z0-9])(?:{v})(?![a-z0-9])", re.I) for k, v in EDITIONS.items()}
SAME_EDITION_UP = 1      # tiers a subtitle naming the file's edition moves up,
EDITION_FLOOR = 1.5      # but not above a hash or line-count match (1)
OTHER_EDITION_TIER = 5   # a subtitle naming another edition: after the rest (4), before machine-translated ones (9)
# an episode tag, SxxEyy, with more episodes after it: Eyy (S01E01E02) or a range -yy / -Eyy (S01E01-02, S01E01-E10);
# a range end must not run into a word (S01E01-1080p, S01E01-10bit)
EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,4})((?:e\d{1,4}|-e?\d{1,4}(?![0-9a-z]))*)(?![0-9])", re.I)
MORE_EPISODES = re.compile(r"(-?)e?(\d{1,4})", re.I)
# a season tag, S03 or Season 3, and a range of seasons it may start: S01-S03, Season 1-3
SEASON = re.compile(r"(?<![a-z0-9])(?:s(\d{1,2})(?:-s(\d{1,2})(?![0-9a-z]))?(?![0-9e])"
                    r"|season[ ._-]?(\d{1,2})(?:-(\d{1,2})(?![0-9a-z]))?(?![0-9]))", re.I)
UNWANTED = re.compile(r"(?<![a-z])(ai[ ._-]?(translated|made|generated)|machine[ ._-]?translat\w*|auto[ ._-]?translat\w*|"
                      r"google[ ._-]?translat\w*|deepl|(?<!non[ ._-])(?<!not[ ._-])(?<!no[ ._-])forced|"
                      r"foreign[ ._-]?parts?([ ._-]?only)?)(?![a-z])", re.I)   # "Non-Forced" is a full subtitle
# a tag written with a hyphen (WEB-DL, WEB-DLRip, Blu-ray, DVD-R, HD-TV), joined before a trailing -GROUP is looked for
HYPHENATED = re.compile(r"(?<![a-z0-9])(web|blu|hd|dvd)-(dlrip|dl|rip|ray|tv|r)(?![a-z0-9])", re.I)


def unwanted(c):
    """AI-made, machine-translated or forced (flagged by the source, or named so in its release)."""
    return bool(c.get("ai")) or bool(UNWANTED.search(c.get("release") or ""))


def _norm(text):
    return " ".join(WORD.findall((text or "").lower()))


def _names_a_release(text):
    """Does a name carry a release's words: a year, a resolution, an episode tag, or a source, codec or other release
    tag (RELEASE_TAGS)?"""
    low = text.lower()
    return bool(YEAR.search(low) or RESOLUTION.search(low) or EPISODE.search(low)) \
        or not RELEASE_TAGS.isdisjoint(WORD.findall(low))


def groups(filename):
    """Release groups a file name names: bracketed names (fansub style) and a trailing `-GROUP` (not a source or quality
    tag: in "WEB-DL", "1080p-WEBRip.x264" or "Bluray-1080p" no group is named), only after a release's words, so the end
    of a hyphenated title ("Ant-Man", "X-Men.2000", "[Judas] Kaguya-sama") is no group."""
    name = release_name(filename)
    out = set()
    for g in BRACKETED.findall(name):
        g = g.strip()
        low = g.lower()
        if HEX.match(g) or low in NOT_GROUPS or any(low in v for v in TAGS.values()) or not re.search(r"[a-z]", low):
            continue
        if re.search(r"\b(1080p|720p|2160p|480p|bd|dvd|web|hevc|x26[45]|flac|aac)\b", low):
            continue   # a tag list like "BD 1080p HEVC"
        out.add(_norm(g))
    plain = HYPHENATED.sub(r"\1\2", BRACKETS.sub("", name)).strip()
    m = TRAILING_GROUP.search(plain)
    if m and _norm(m.group(1)).split()[0] not in RELEASE_TAGS and _names_a_release(plain[:m.start()]):
        out.add(_norm(m.group(1)))
    return {g for g in out if len(g) >= 2}


def editions(text):
    """The editions a file or release name names, as whole words: "dc" (DC, Director's Cut), "extended", "roadshow",
    "unrated", "theatrical"."""
    return {k for k, rx in EDITION.items() if rx.search(text or "")}


def episodes(text):
    """Every episode tag of a name as (season, first, last): S01E05 gives [(1, 5, 5)], S01E05E06 [(1, 5, 5), (1, 6, 6)],
    S01E01-E10 and S01E01-10 [(1, 1, 1), (1, 1, 10)]; [] when it names no episode."""
    out = []
    for m in EPISODE.finditer(text or ""):
        season, prev = int(m.group(1)), int(m.group(2))
        out.append((season, prev, prev))
        for dash, num in MORE_EPISODES.findall(m.group(3)):
            num = int(num)
            out.append((season, min(prev, num), max(prev, num)) if dash else (season, num, num))
            prev = num
    return out


def season_episode(text):
    """(season, episode) a name gives: S01E01 gives (1, 1) (its first episode tag); S03 or "Season 3" gives (3, None)
    (S01-S03 its first season, 1); else (None, None)."""
    tags = episodes(text)
    if tags:
        return tags[0][0], tags[0][1]
    m = SEASON.search(text or "")
    if m:
        return int(m.group(1) or m.group(3)), None
    return None, None


def seasons(text):
    """Every season a name's season tags give: S03 or "Season 3" gives {3}; S01.S02.S03, S01-S03 and Season 1-3 give
    {1, 2, 3}; set() when it names none."""
    out = set()
    for m in SEASON.finditer(text or ""):
        first = int(m.group(1) or m.group(3))
        last = int(m.group(2) or m.group(4) or first)
        out.update(range(min(first, last), max(first, last) + 1))
    return out


def other_episode(release, ep):
    """The release names another season than the file's (ep: the file's (season, episode)), or other episodes
    of the same season and not the file's. A pack of the file's own season (or of several, the file's among them:
    S01-S03), a release whose episodes include the file's, or a name with no season, is not another."""
    if not ep or ep[0] is None:
        return False
    tags = episodes(release)
    if tags:
        return not any(s == ep[0] and (ep[1] is None or a <= ep[1] <= b) for s, a, b in tags)
    named = seasons(release)
    return bool(named) and ep[0] not in named


def source_kind(text):
    """The kind of source a name gives (TAGS: remux, bluray, web, tv, dvd), read after its title: from its first year,
    episode or season tag on, or the whole name when it has none, so a title's own word isn't taken for one
    ("Charlottes.Web.2006.DVDRip" is a DVD rip). A tag written with a hyphen counts as written and joined
    ("web-dl"; "HD-TV" as "hdtv")."""
    low = (text or "").lower().replace(".", " ").replace("_", " ")
    starts = [m.start() for m in (YEAR.search(low), EPISODE.search(low), SEASON.search(low)) if m]
    low = low[min(starts):] if starts else low
    words = (set(re.findall(r"[a-z0-9-]+", low)) | set(WORD.findall(low))   # "web-dl" and, in "bluray-1080p", "bluray"
             | set(WORD.findall(HYPHENATED.sub(r"\1\2", low))))
    for kind in ("remux", "bluray", "web", "tv", "dvd"):
        if words & set(TAGS[kind]):
            return kind
    return None


# what the ranking reads of the file, once for all its candidates: its release groups, kind of source, hash (lower
# case), editions and (season, episode)
_File = namedtuple("_File", "groups kind hash editions episode")


def _file_facts(filename, file_hash):
    return _File(groups(filename), source_kind(filename), (file_hash or "").lower(), editions(filename),
                 season_episode(filename))


def _release_tier(c, f, ref_cues):
    rel = " " + _norm(c.get("release")) + " "
    if (f.hash and c.get("hash") == f.hash) or c.get("hash_match"):   # hash_match: the source matched this file's hash
        return 1
    if ref_cues and c.get("cues") and abs(c["cues"] - ref_cues) <= 0.02 * ref_cues:
        return 1   # as many lines as the release's own track: most likely made from this release
    if other_episode(c.get("release"), f.episode):
        return 4   # another season or episode: no release-group or same-source credit
    if c.get("release_match") or (f.groups and any(f" {g} " in rel for g in f.groups)):
        return 2   # found by the file's release name (TheSubtitleDB), or it names the file's release group
    if f.kind and source_kind(c.get("release")) == f.kind:
        return 3
    return 4


def _tier(rank, c, f, ref_cues, viewer_first):
    if rank == 0 and viewer_first:
        return 0
    if unwanted(c):
        return 9
    tier = _release_tier(c, f, ref_cues)
    theirs = editions(c.get("release")) if f.editions and tier > 1 else None
    if not theirs:     # no edition named by the file or the subtitle, or a hash or line-count match: as it is
        return tier
    return max(tier - SAME_EDITION_UP, EDITION_FLOOR) if theirs & f.editions else OTHER_EDITION_TIER


def tier_of(rank, c, filename=None, file_hash=None, ref_cues=None, viewer_first=True):
    """How likely a candidate is to fit the file as it is: 0 the viewer's first, 1 a hash or line-count match, 2 its
    release (by name or group), 3 the same kind of source, 4 the rest, 9 AI-made, machine-translated or forced; a
    subtitle naming the file's edition a tier up (1.5 from 2), one naming another 5."""
    return _tier(rank, c, _file_facts(filename, file_hash), ref_cues, viewer_first)


def order(candidates, filename=None, file_hash=None, skip=False, ref_cues=None, viewer_first=True):
    """candidates: in the viewer's order, each with optional `release`, `hash`, `hash_match`, `release_match`, `ai`.
    Returns [(viewer rank, cand)] in the order to try them; with viewer_first off the viewer's first has no place of its
    own (the first is then the likeliest to fit the file as it is: the guess while nothing can be checked, through
    Worker._name_order). Ties keep the list's order: the config's subtitles first, then the built-in sources
    (srclimits.SOURCE_ORDER)."""
    facts = _file_facts(filename, file_hash)
    ranked = [(r, c) for r, c in enumerate(candidates) if not (skip and unwanted(c))]
    return sorted(ranked, key=lambda rc: (_tier(rc[0], rc[1], facts, ref_cues, viewer_first), rc[0]))

