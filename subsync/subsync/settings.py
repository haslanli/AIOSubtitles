"""Settings from the environment. The source config's address and the addon key are secrets and are never logged."""
import base64
import binascii
import hashlib
import json
import math
import os
import socket
import struct

from . import ALGO_VERSION, RULE_REVISION
from .worker import lang_list


def _on(value):
    return str(value).strip().lower() in ("1", "on", "true", "yes")


ROUTE_FILE = "/proc/net/route"            # Linux: the kernel's IPv4 routing table
CPU_MAX_FILE = "/sys/fs/cgroup/cpu.max"   # cgroup v2: the container's processor limit ("max 100000" when none)


def default_gateway(path=None):
    """This machine's default IPv4 gateway (inside a Docker container: the address its network reaches it from), or
    None when there is none or it can't be read."""
    try:
        with open(path or ROUTE_FILE, encoding="ascii") as f:
            rows = f.read().splitlines()[1:]
    except (OSError, ValueError):
        return None
    for row in rows:
        fields = row.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            gateway, flags = int(fields[2], 16), int(fields[3], 16)
        except ValueError:
            continue
        if flags & 0x2 and gateway:          # RTF_GATEWAY
            return socket.inet_ntoa(struct.pack("<L", gateway))
    return None


def _processors():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count()


def available_cpus(cpu_max=None, count=None):
    """The processors this process may use: the ones it may run on, within the container's limit, at least 1."""
    n = (count or _processors)() or 1
    try:
        with open(cpu_max or CPU_MAX_FILE, encoding="ascii") as f:
            quota, period = f.read().split()[:2]
        if quota != "max":
            n = min(n, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, n)


def _places(value):
    """SUBSYNC_SYNCS_AT_ONCE: a whole number from 1, or auto (the processors available); None when it is neither."""
    value = str(value).strip().lower()
    if value == "auto":
        return available_cpus()
    return int(value) if value.isdigit() and int(value) >= 1 else None


def _language_limit(value):
    """SUBSYNC_MAX_LANGUAGES: a whole number from 1 to 4 (the pages name four places at most); None otherwise."""
    value = str(value).strip()
    return int(value) if value.isdigit() and 1 <= int(value) <= 4 else None


class Settings:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        g = lambda k, d: e.get(k, d)  # noqa: E731
        self.port = int(g("SUBSYNC_PORT", "8790"))
        self.data = g("SUBSYNC_DATA", "/data")
        # where players fetch the synced files, and the AIOStreams config subsync finds files through: no defaults
        self.public_base = g("SUBSYNC_PUBLIC_BASE", "").rstrip("/")
        self.source_manifest = g("SUBSYNC_SOURCE_MANIFEST", "")
        self.source_password = g("SUBSYNC_SOURCE_PASSWORD", "")    # subsync reads its config with it: the Real-Debrid check
        self.languages = lang_list(g("SUBSYNC_LANGUAGES", "eng"))
        self.max_tries = int(g("SUBSYNC_MAX_TRIES", "6"))                      # subtitles tried per language and file
        self.cap_bytes = int(float(g("SUBSYNC_STORE_CAP_MB", "2048")) * 1024 * 1024)
        self.daily_budget = int(float(g("SUBSYNC_DAILY_BUDGET_GB", "50")) * 1e9)
        self.backoff_s = int(g("SUBSYNC_403_BACKOFF_S", "900"))
        self.retry_stopped_s = int(g("SUBSYNC_RETRY_STOPPED_S", "600"))
        # the audio route: a file with no text or image subtitle track is timed from its speech, reading audio windows
        # within SUBSYNC_AUDIO_BUDGET_GB per file (shorter windows on high-bitrate files, none when they would be
        # too short); off, every file is read index only (SUBSYNC_INDEX_CAP_MB)
        self.audio_route = _on(g("SUBSYNC_AUDIO_ROUTE", "off"))
        self.audio_budget = int(float(g("SUBSYNC_AUDIO_BUDGET_GB", "1")) * 1e9) or None
        self.file_cap = int(float(g("SUBSYNC_FILE_CAP_GB", "6")) * 1e9) or None
        # the audio route starts only when the viewer downloads subsync's own entry for the file (proof of play): a plain
        # subtitle request also comes for files nobody played (AIOMetadata asks for the first listed version). Off: the
        # audio route starts on any request, as before.
        self.audio_on_pick = _on(g("SUBSYNC_AUDIO_ON_PICK", "on"))
        # a pick (the "(Subsync)" entry's download) waits this long for the file's result, like AutoSync; AIOMetadata
        # gives up on a subtitle after 15 s
        self.pick_wait = float(g("SUBSYNC_PICK_WAIT_MS", "14000")) / 1000
        # Every candidate judged, the best fit offered first (up to SUBSYNC_MAIN_ALTERNATIVES for the first language)
        self.best_fit = _on(g("SUBSYNC_BEST_FIT", "on"))
        self.alternatives = max(1, int(g("SUBSYNC_MAIN_ALTERNATIVES", "3")))
        self.skip_ai = _on(g("SUBSYNC_SKIP_AI", "on"))                     # AI-made, machine-translated, forced: never tried
        self.prefer_plain = _on(g("SUBSYNC_PREFER_PLAIN_TRACK", "on"))     # the plain track as the reference, not the SDH one
        # Sources subsync searches itself (extra.py): the keyless ones by default; SubDL,
        # SubSource and Wyzie only with a key (in public mode the service's keys, and never Wyzie); TheSubtitleDB's key
        # only raises its download limit
        self.extra_sources = [x.strip().lower() for x in g("SUBSYNC_EXTRA_SOURCES", "osv3,osv3plus,tsdb,gestdown").split(",")
                              if x.strip()]
        self.subdl_key = g("SUBSYNC_SUBDL_KEY", "").strip()
        self.subsource_key = g("SUBSYNC_SUBSOURCE_KEY", "").strip()
        self.tsdb_key = g("SUBSYNC_TSDB_KEY", "").strip()
        self.wyzie_key = g("SUBSYNC_WYZIE_KEY", "").strip()
        self.source_timeout = float(g("SUBSYNC_SOURCE_TIMEOUT_S", "5"))
        # a source the AIOStreams config already has as a subtitle addon is asked once, through the config
        self.skip_config_sources = _on(g("SUBSYNC_SKIP_CONFIG_SOURCES", "on"))
        self.osv3_url = g("SUBSYNC_OSV3_URL", "https://opensubtitles-v3.strem.io").rstrip("/")
        self.subsource_url = g("SUBSYNC_SUBSOURCE_URL", "https://api.subsource.net/api/v1").rstrip("/")
        self.tsdb_url = g("SUBSYNC_TSDB_URL", "https://api.thesubtitledb.org").rstrip("/")
        self.cinemeta_url = g("SUBSYNC_CINEMETA_URL", "https://v3-cinemeta.strem.io").rstrip("/")
        self.gestdown_url = g("SUBSYNC_GESTDOWN_URL", "https://api.gestdown.info").rstrip("/")
        self.subdl_url = g("SUBSYNC_SUBDL_URL", "https://api.subdl.com").rstrip("/")
        self.subdl_download_url = g("SUBSYNC_SUBDL_DOWNLOAD_URL", "https://dl.subdl.com").rstrip("/")
        self.wyzie_url = g("SUBSYNC_WYZIE_URL", "https://sub.wyzie.io").rstrip("/")
        self.osv3plus_url = g("SUBSYNC_OSV3PLUS_URL", "https://opensubtitles.stremio.homes").rstrip("/")
        # best fit: originals checked (no alass) for up to this many candidates; alass only on SUBSYNC_MAX_TRIES of them
        self.gate_tries = max(1, int(g("SUBSYNC_GATE_TRIES", "20")))
        self.index_cap = int(float(g("SUBSYNC_INDEX_CAP_MB", "16")) * 1024 * 1024)
        self.audio_lead = float(g("SUBSYNC_AUDIO_LEAD_MS", "190")) / 1000
        # a "(synced)" entry for a file still being worked on, served as the original until the copy is ready
        self.pending_entry = _on(g("SUBSYNC_PENDING_ENTRY", "off"))
        self.pending_wait = float(g("SUBSYNC_PENDING_WAIT_MS", "1000")) / 1000
        self.url_rewrite = g("SUBSYNC_URL_REWRITE", "")
        self.read_timeout = float(g("SUBSYNC_READ_TIMEOUT_S", "60"))
        self.size_slack = int(float(g("SUBSYNC_SIZE_SLACK_MB", "5.5")) * 1024 * 1024)   # listed sizes are rounded to 0.01 GiB
        self.alass = g("SUBSYNC_ALASS", "alass")
        self.ffmpeg = g("SUBSYNC_FFMPEG", "ffmpeg")
        self.snap = _on(g("SUBSYNC_SNAP", "on"))                              # per-line snap after alass
        self.snap_within = float(g("SUBSYNC_SNAP_WITHIN", "0.95"))           # a snapped copy's bar for "within 200 ms"
        # the second way to pass: a re-timed copy clearly closer to the release's track than the original
        self.better = {"paired": float(g("SUBSYNC_BETTER_PAIRED", "0.85")),
                       "median_ms": float(g("SUBSYNC_BETTER_MEDIAN_MS", "200")),
                       "ratio": float(g("SUBSYNC_BETTER_RATIO", "0.5")),
                       # against another language's track: the alignment's median error (relgate.aligned)
                       "cross_median_ms": float(g("SUBSYNC_BETTER_CROSS_MEDIAN_MS", "300")),
                       "cross_paired": float(g("SUBSYNC_BETTER_CROSS_PAIRED", "0.75"))} \
            if _on(g("SUBSYNC_BETTER", "on")) else None
        # with no track in a subtitle's language, a track in one of these languages is its reference first
        self.fallback_reference = tuple(lang_list(g("SUBSYNC_FALLBACK_REFERENCE", "eng")))
        # the audio route's second way to pass: a re-timed copy clearly closer to the speech than the original
        self.audio_better = {"matched": int(g("SUBSYNC_AUDIO_BETTER_MATCHED", "25")),
                             "miss": float(g("SUBSYNC_AUDIO_BETTER_MISS", "0.30")),
                             "median_ms": float(g("SUBSYNC_AUDIO_BETTER_MEDIAN_MS", "150")),
                             "within": float(g("SUBSYNC_AUDIO_BETTER_WITHIN", "0.55")),
                             "min_shift_s": float(g("SUBSYNC_AUDIO_BETTER_MIN_SHIFT_S", "1.0"))} \
            if _on(g("SUBSYNC_AUDIO_BETTER", "on")) else None
        self.gate_splits = _on(g("SUBSYNC_GATE_SPLITS", "on"))               # a line that splits a release line counts as paired
        self.rank_by_release = _on(g("SUBSYNC_RANK_BY_RELEASE", "on"))       # subtitles made for this release tried first
        self.anchored_guess = _on(g("SUBSYNC_ANCHORED_GUESS", "on"))         # a waiting file's other languages: shifted onto the main guess
        self.list_in_sync = _on(g("SUBSYNC_LIST_IN_SYNC", "on"))             # an in-sync original further down listed as is
        self.image_ref = _on(g("SUBSYNC_IMAGE_REF", "on"))                   # image subtitles' cue times as the reference
        self.fast_path = _on(g("SUBSYNC_FAST_PATH", "on"))                   # originals checked first, in-sync listed at once
        self.shift_path = _on(g("SUBSYNC_SHIFT_PATH", "on"))                 # then one steady offset (shift.py), alass after
        # Lines paired by their words where timing hasn't settled a language (textpair.py; D1, D2)
        self.text_pairing = _on(g("SUBSYNC_TEXT_PAIRING", "on"))
        self.sections = _on(g("SUBSYNC_SECTIONS", "on"))                    # D4: other cuts fitted in sections
        self.audio_extra = max(0, int(g("SUBSYNC_AUDIO_EXTRA_WINDOWS", "12")))   # D5: windows a close call may add
        self.parallel_languages = _on(g("SUBSYNC_PARALLEL_LANGUAGES", "on")) # languages at the same time
        # a private path key for the routes that start work or show status (manifest, subtitles, status), so a published
        # port or a reverse-proxy route can't be used to make subsync read files; the synced files and /health stay
        # open. And an optional path prefix (e.g. /subsync) that a reverse proxy forwards unchanged.
        self.addon_key = g("SUBSYNC_ADDON_KEY", "").strip().strip("/")
        base = g("SUBSYNC_BASE_PATH", "").strip().strip("/")
        self.base_path = "/" + base if base else ""
        # rows of these addons (comma-separated names) are never read (empty by default)
        self.skip_addons = tuple(n.strip() for n in g("SUBSYNC_SKIP_ADDONS", "").split(",") if n.strip())
        # public mode: one service for many people, each signed up with their own AIOStreams (public.py)
        self.public = _on(g("SUBSYNC_PUBLIC", "off"))
        self.secret_key = g("SUBSYNC_SECRET_KEY", "").strip()                  # encrypts users' records; never logged
        self.invite = g("SUBSYNC_PUBLIC_INVITE", "").strip()
        self.max_users = int(g("SUBSYNC_MAX_USERS", "200"))
        self.signups_per_hour = int(g("SUBSYNC_SIGNUPS_PER_HOUR", "5"))      # per address
        self.user_files_per_day = int(g("SUBSYNC_USER_FILES_PER_DAY", "60"))
        self.user_daily_bytes = int(float(g("SUBSYNC_USER_DAILY_MB", "1000")) * 1024 * 1024)
        self.user_queue = int(g("SUBSYNC_USER_QUEUE", "3"))
        # the most languages a person may choose on the sign-up and settings pages
        self.max_languages = _language_limit(g("SUBSYNC_MAX_LANGUAGES", "4"))
        self.active_workers = int(g("SUBSYNC_ACTIVE_WORKERS", "100"))
        self.worker_idle_s = float(g("SUBSYNC_WORKER_IDLE_MIN", "30")) * 60
        self.user_keep_days = int(g("SUBSYNC_USER_KEEP_DAYS", "180"))
        # each person's AIOStreams checked again, at most this often, while they use the service (recheck.py); 0: never
        self.recheck_hours = float(g("SUBSYNC_RECHECK_HOURS", "24"))
        # the web server's addresses; "gateway" is this container's default gateway, found at start (the address a
        # web server reaching subsync through a port Docker publishes comes from), so no fixed Docker network is needed
        proxies = [a.strip() for a in g("SUBSYNC_TRUSTED_PROXY", "127.0.0.1").split(",") if a.strip()]
        self.gateway_missing = False
        trusted = []
        for proxy in proxies:
            if proxy.lower() == "gateway":
                gateway = default_gateway()
                self.gateway_missing = gateway is None
                proxy = gateway
            if proxy and proxy not in trusted:
                trusted.append(proxy)
        self.trusted_proxy = tuple(trusted)
        self.max_connections = int(g("SUBSYNC_MAX_CONNECTIONS", "256"))
        # files worked on at once for everyone (each person's worker takes a place of one shared turn)
        self.syncs_at_once = _places(g("SUBSYNC_SYNCS_AT_ONCE", "auto"))
        # Usenet: off by default. On: files are also looked for through a second config, the Usenet
        # config, when the sync config doesn't have them, at most SUBSYNC_USENET_LISTINGS_PER_DAY times a day (per
        # person in public mode, where "on" lets each person turn it on for themselves and their record holds the
        # Usenet config's link); its reads take the index cap and never the audio route
        self.usenet = _on(g("SUBSYNC_USENET", "off"))
        self.usenet_listings_per_day = max(0, int(g("SUBSYNC_USENET_LISTINGS_PER_DAY", "10")))
        self.usenet_manifest = g("SUBSYNC_USENET_MANIFEST", "")
        self.usenet_password = g("SUBSYNC_USENET_PASSWORD", "")

    def secret_bytes(self):
        """The public mode's secret: base64url of at least 32 random bytes. ValueError otherwise."""
        try:
            raw = base64.b64decode(self.secret_key + "=" * (-len(self.secret_key) % 4), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("SUBSYNC_SECRET_KEY is not base64url") from None
        if len(raw) < 32:
            raise ValueError("SUBSYNC_SECRET_KEY is shorter than 32 bytes")
        return raw

    @property
    def rule_id(self):
        """The pass rule a verdict was judged by: the code version and every setting that changes a verdict. A stored
        "keep original" judged under another one is judged again."""
        rule = {"algo": ALGO_VERSION, "revision": RULE_REVISION, "better": self.better, "audio_better": self.audio_better, "snap": self.snap,
                "snap_within": self.snap_within, "gate_splits": self.gate_splits, "image_ref": self.image_ref,
                "audio_lead": self.audio_lead, "audio_budget": self.audio_budget, "prefer_plain": self.prefer_plain,
                "shift_path": self.shift_path, "fallback_reference": self.fallback_reference,
                "text_pairing": self.text_pairing, "sections": self.sections, "audio_extra": self.audio_extra}
        return hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:12]

    def missing(self):
        """What must be set before the service can work. In public mode each user's record holds their sync config's
        link and password, so the service needs its secret and its address instead."""
        if self.public:
            try:
                self.secret_bytes()
                secret_ok = True
            except ValueError:
                secret_ok = False
            return [name for name, ok in (("SUBSYNC_SECRET_KEY", secret_ok),
                                          ("SUBSYNC_PUBLIC_BASE", bool(self.public_base))) if not ok]
        # a setup with Usenet only finds files and subtitles through its Usenet config alone
        usenet_only = bool(self.usenet and self.usenet_manifest and self.usenet_password)
        return [name for name, value in (("SUBSYNC_SOURCE_MANIFEST", self.source_manifest or usenet_only),
                                         ("SUBSYNC_SOURCE_PASSWORD", self.source_password or usenet_only),
                                         ("SUBSYNC_PUBLIC_BASE", self.public_base)) if not value]

    def conflicts(self):
        """Settings that can't go together, in plain sentences (empty: none)."""
        out = []
        if self.public and self.audio_route:
            out.append("The audio route is off in public mode: set SUBSYNC_AUDIO_ROUTE=off or turn public mode off.")
        if self.public and self.wyzie_key:
            out.append("Wyzie Subs isn't used in public mode (one free key per network, paused when used from servers): "
                       "remove SUBSYNC_WYZIE_KEY or turn public mode off.")
        if self.syncs_at_once is None:
            out.append("SUBSYNC_SYNCS_AT_ONCE must be a whole number from 1, or auto.")
        if self.max_languages is None:
            out.append("SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to 4.")
        if self.public and self.gateway_missing:
            out.append("SUBSYNC_TRUSTED_PROXY says gateway, but this machine has no default gateway to trust: give your "
                       "web server's address instead.")
        return out
