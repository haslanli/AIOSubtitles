"""Fixed values for the subtitle sources AIOSubtitles asks itself, in one place: their order,
the limits each site publishes, how many subtitles a file may take from a site with a tight limit, when a failing site
is paused, how much is asked and read of a site, how long answers are kept, and how a source in a user's AIOStreams
config is recognised. (Every subtitle download's own bounds, its size and its pacing, are in sources.py.)

The limits are the sites' own published numbers, read 2026-11-12 (`docs/server/aiosubtitles-builtin-sources-plan.md`):
TheSubtitleDB downloads 1,000 an hour without a key, 100,000 a day with one (lookups unlimited); SubDL 2,000 requests a
day per key and 300 downloads a day per address without a paid key; SubSource 60 a minute, 1,800 an hour, 7,200 a day
per free key; Wyzie 1,000 requests a day per free key. A site without published numbers has no entry here: its own
429 answers pause it (sourcestate.Health).
"""

# The built-in sources, in the order they are asked and, among equally likely subtitles, tried: the ones that match the
# played file by its hash or know a subtitle's line count first.
SOURCE_ORDER = ("osv3", "osv3plus", "tsdb", "subdl", "subsource", "gestdown", "wyzie")
KEYLESS = ("osv3", "osv3plus", "tsdb", "gestdown")          # asked without any key (SUBSYNC_EXTRA_SOURCES)
KEYED = tuple(s for s in SOURCE_ORDER if s not in KEYLESS)  # asked only with a key, each within its request limits

# (source, kind, with a key?) -> [(window in seconds, how many)]. Windows are aligned to the clock (a day is a UTC day).
LIMITS = {
    ("tsdb", "download", False): [(3600, 1000)],
    ("tsdb", "download", True): [(86400, 100000)],
    ("subdl", "request", True): [(86400, 2000)],
    ("subdl", "download", False): [(86400, 300)],
    ("subdl", "download", True): [(86400, 300)],             # a free key doesn't lift the per-address download limit
    ("subsource", "request", True): [(60, 60), (3600, 1800), (86400, 7200)],
    ("wyzie", "request", True): [(86400, 1000)],
}
# SubSource counts a download as a request; the others count downloads apart.
DOWNLOAD_KIND = {"subsource": "request", "wyzie": "request"}

# Sites whose downloads are zips subsync unpacks (sources.fetch_subtitle): a player can't use their address as it is.
ZIPPED = ("subdl", "subsource")
# Per file and language, at most this many subtitles are downloaded from a site with a tight limit.
PER_FILE_DOWNLOADS = {"subdl": 3, "subsource": 4, "wyzie": 2}
# Public mode: at this share of a window's limit, a person who already used more than their share gets that site only
# for the subtitles most likely to fit (a hash, line-count or release match); their share is the window's limit divided
# by the people who used the site in that window, never under MIN_SHARE of the limit.
FAIR_SHARE_AT = 0.8
MIN_SHARE = 0.10
TOP_TIER = 2                       # choose.tier: 0 the viewer's first, 1 a hash or line-count match, 2 the release group

# A source that fails PAUSE_FAILURES times within PAUSE_WINDOW_S is skipped for PAUSE_S, then tried with one request.
PAUSE_FAILURES = 3
PAUSE_WINDOW_S = 600
PAUSE_S = 600
RETRY_AFTER_MIN_S = 60             # a site's own Retry-After pauses it at least this long,
RETRY_AFTER_MAX_S = 3600           # and at most this long

# A site's answer is read up to ANSWER_MAX_BYTES, its request waits at least MIN_TIMEOUT_S (a shorter
# SUBSYNC_SOURCE_TIMEOUT_S still bounds how long a file waits for the answers)
ANSWER_MAX_BYTES = 4 * 1024 * 1024
MIN_TIMEOUT_S = 5.0

# Answers kept for every person: a found list for SEARCH_TTL_S, "nothing found" for EMPTY_TTL_S.
SEARCH_TTL_S = 6 * 3600
EMPTY_TTL_S = 3600
SEARCH_CACHE_ENTRIES = 2000
CALLS_AT_ONCE = 16                 # source calls running at the same time, for every file together
# Telling duplicates apart (subs.fingerprint): at most FINGERPRINT_CREDIT_LINES credit lines left out
# at each end; a subtitle of more than FINGERPRINT_LONGER_THAN dialogue lines also leaves out FINGERPRINT_EDGE lines at
# each end (sites add notices there)
FINGERPRINT_CREDIT_LINES = 3
FINGERPRINT_LONGER_THAN = 20
FINGERPRINT_EDGE = 3
TSDB_RELEASE_SCORE = 0.8          # TheSubtitleDB's release-name match: at least this score is a match (0.57 was another film)
TSDB_PAGES = 5                     # TheSubtitleDB pages by IMDb id; the torrent and release lookups take one
TSDB_PAGE_SIZE = 100               # subtitles a page: TheSubtitleDB's
SUBDL_PAGE_SIZE = 30               # SubDL's
SUBSOURCE_PAGE_SIZE = 100          # SubSource's (one page)
# Public mode: a person's copies rebuilt from their originals are kept this long, at most this many, for them only;
# the subtitles of their lists, by id, for finding a stored copy's original again
TEXT_KEEP_S = 3600
TEXT_KEEP_ENTRIES = 50
BY_ID_ENTRIES = 5000

# AIOStreams subtitle presets that are the same source as a built-in one (asked once, through the config).
CONFIG_PRESETS = {"opensubtitles": "osv3", "opensubtitles-v3-plus": "osv3plus", "subdl": "subdl",
                  "subsource": "subsource", "subhero": "wyzie"}
# A subtitle's address -> its source (for a config's custom addons and the subtitles a config lists).
HOSTS = (("opensubtitles-v3.strem.io", "osv3"), ("strem.io", "osv3"), ("stremio.homes", "osv3plus"),
         ("thesubtitledb.org", "tsdb"), ("subdl.com", "subdl"), ("subsource.net", "subsource"),
         ("gestdown.info", "gestdown"), ("wyzie.io", "wyzie"))
# The sign-up's clean-up may remove these presets: the public service asks each of them itself (no Wyzie there).
CLEANABLE_PRESETS = ("opensubtitles", "opensubtitles-v3-plus", "subdl", "subsource")


def source_of_url(url):
    """The built-in source a subtitle address belongs to, or None."""
    from urllib.parse import urlsplit
    host = (urlsplit(str(url or "")).hostname or "").lower()
    for suffix, source in HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return source
    return None


def config_sources(config):
    """The built-in sources an AIOStreams config already has as enabled subtitle addons: by preset type, or, for a
    custom addon, by its address."""
    out = set()
    for p in (config or {}).get("presets") or []:
        if not isinstance(p, dict) or p.get("enabled") is False:
            continue
        options = p.get("options") or {}
        resources = options.get("resources")
        if isinstance(resources, list) and resources and "subtitles" not in resources:
            continue
        source = CONFIG_PRESETS.get(p.get("type"))
        if source is None and p.get("type") == "custom":
            source = source_of_url(options.get("manifestUrl") or options.get("url"))
        if source:
            out.add(source)
    return out
