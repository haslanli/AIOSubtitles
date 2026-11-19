"""Settings from the environment. The source config's address and the addon key are secrets and are never logged.

Every default is the documented one (explain.SETTINGS). No packages besides the standard library, so hostcmd, which
only reads settings, loads none of the sync's code (numpy, the worker)."""
import base64
import binascii
import hashlib
import json
import math
import os
import socket
import struct

from . import ALGO_VERSION, RULE_REVISION, explain
from .aiosconfig import MAX_LANGUAGES
from .langs import known, lang_list

# The units of the size settings, each chosen once and kept (changing one would change every limit set in it): a *_MB
# setting counts MiB, a *_GB setting 10^9 bytes. A figure shown for a setting is shown in that setting's unit.
MB = 1024 * 1024
GB = 10 ** 9


def _on(value):
    return str(value).strip().lower() in ("1", "on", "true", "yes")


def _switch(value):
    """An on/off setting: True for on, False for off or empty; None for any other word ("enabled" or a typo
    read as off without a word, even for a setting that is on by default)."""
    value = str(value).strip().lower()
    return True if _on(value) else False if value in ("", "0", "off", "false", "no") else None


def _default(name):
    """A setting's documented default, written once in explain.SETTINGS; "" for a required setting (it has none)."""
    return explain.SETTINGS[name][0] or ""


ROUTE_FILE = "/proc/net/route"            # Linux: the kernel's IPv4 routing table
CPU_MAX_FILE = "/sys/fs/cgroup/cpu.max"   # cgroup v2: the container's processor limit ("max 100000" when none)
CFS_DIR = "/sys/fs/cgroup/cpu"            # cgroup v1: the same limit, cpu.cfs_quota_us ("-1" when none) and period


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


def _cpu_limit(cpu_max, cfs_dir):
    """The container's processor limit as (quota, period): cgroup v2's cpu.max, or on a cgroup v1 host (no cpu.max) its
    two files; the quota is "max" or "-1" when there is none."""
    try:
        with open(cpu_max, encoding="ascii") as f:
            return f.read().split()[:2]
    except FileNotFoundError:
        pass
    with open(os.path.join(cfs_dir, "cpu.cfs_quota_us"), encoding="ascii") as quota, \
            open(os.path.join(cfs_dir, "cpu.cfs_period_us"), encoding="ascii") as period:
        return quota.read().strip(), period.read().strip()


def available_cpus(cpu_max=None, cfs_dir=None, processors=None):
    """The processors this process may use: the ones it may run on, within the container's limit, at least 1."""
    n = (processors or os.process_cpu_count)() or 1
    try:
        quota, period = _cpu_limit(cpu_max or CPU_MAX_FILE, cfs_dir or CFS_DIR)
        if quota not in ("max", "-1"):
            n = min(n, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, n)


def _whole(value, least=1, most=None):
    """A whole number from `least` (to `most`) written in digits; None otherwise. isdecimal, not isdigit, which also
    takes the superscripts int() refuses ("2²")."""
    value = str(value).strip()
    if not value.isdecimal():
        return None
    try:
        n = int(value)
    except ValueError:                      # more digits than int() converts
        return None
    return n if n >= least and (most is None or n <= most) else None


def _number(value, above_zero=False, scale=1):
    """A number from 0 (above 0 with `above_zero`) times `scale`, finite; None otherwise ("nan", "inf", or a value too
    large to count in `scale`'s unit)."""
    try:
        n = float(str(value).strip()) * scale
    except ValueError:
        return None
    return n if math.isfinite(n) and (n > 0 if above_zero else n >= 0) else None


def _like(name):
    """", like <the setting's default>" for the sentence of a value written wrong; "" for a setting with none."""
    return f", like {_default(name)}" if _default(name) else ""


class _Reader:
    """The number and on/off settings, each read one way: a value that isn't a number in
    the setting's range (written wrong, or one its explanation rules out), or isn't on or off, is noted in `wrong`, in
    the sentence conflicts() says, and read as the setting's default (0 for one with none)."""

    def __init__(self, get):
        self.get, self.wrong = get, []

    def whole(self, name, least=0, most=None):
        words = f"a whole number from {least}" + (f" to {most}" if most is not None else _like(name))
        return self._read(name, lambda v: _whole(v, least, most), words)

    def number(self, name, above_zero=False, scale=1):
        """The number times `scale` (a size's unit, or a time's)."""
        words = ("a number above 0" if above_zero else "a number from 0") + _like(name)
        return self._read(name, lambda v: _number(v, above_zero, scale), words)

    def switch(self, name):
        return self._read(name, _switch, "on or off")

    def _read(self, name, parse, words):
        value = parse(self.get(name))
        if value is None:
            self.wrong.append(f"{name} must be {words}.")
            value = parse(_default(name) or "0")
        return value


def _places(value):
    """SUBSYNC_SYNCS_AT_ONCE: a whole number from 1, or auto (the processors available); None when it is neither."""
    value = str(value).strip().lower()
    return available_cpus() if value == "auto" else _whole(value)


def _audio_at_once(value):
    """SUBSYNC_AUDIO_AT_ONCE: auto, or a whole number from 1; None when it is neither."""
    value = str(value).strip().lower()
    return "auto" if value == "auto" else _whole(value)


def _audio_when(env):
    """SUBSYNC_AUDIO_WHEN (pick or open; None when it is neither); when it isn't set, the setting it replaced,
    SUBSYNC_AUDIO_ON_PICK, still read: off means open."""
    value = env.get("SUBSYNC_AUDIO_WHEN")
    if value is None or not str(value).strip():
        return "pick" if _on(env.get("SUBSYNC_AUDIO_ON_PICK", _default("SUBSYNC_AUDIO_ON_PICK"))) else "open"
    value = str(value).strip().lower()
    return value if value in ("pick", "open") else None


# A person's answer to "files with no subtitles of their own" (public mode, kept in their record), behind the
# instance's audio route: never, after their pick, or as soon as a file is opened
AUDIO_CHOICES = ("off", "pick", "open")


def _language_limit(value):
    """SUBSYNC_MAX_LANGUAGES: a whole number from 1 to MAX_LANGUAGES (the pages name four places at most); None
    otherwise."""
    return _whole(value, most=MAX_LANGUAGES)


def _overrides(value):
    """SUBSYNC_ORIGINAL_LANGUAGES as ({title: language}, the number of entries left out): a title is an IMDb id, or mal:
    or kitsu: and a number (origlang.parse_overrides)."""
    from .origlang import parse_overrides
    return parse_overrides(value)


class Settings:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        g = lambda k: e.get(k, _default(k))  # noqa: E731
        read = _Reader(g)
        self.port = read.whole("SUBSYNC_PORT", 1, 65535)
        self.bind = g("SUBSYNC_BIND").strip()      # the address it listens on (127.0.0.1 behind a host-network proxy)
        self.data = g("SUBSYNC_DATA")
        # where players fetch the synced files, and the AIOStreams config subsync finds files through: no defaults
        self.public_base = g("SUBSYNC_PUBLIC_BASE").rstrip("/")
        self.source_manifest = g("SUBSYNC_SOURCE_MANIFEST")
        self.source_password = g("SUBSYNC_SOURCE_PASSWORD")  # subsync reads its config with it: the Real-Debrid check
        languages = g("SUBSYNC_LANGUAGES")
        self.languages = lang_list(languages)
        # the codes there that name no language: a self-hosted install doesn't start with one (conflicts)
        self.unknown_languages = [c.strip() for c in languages.split(",") if c.strip() and not known(c)]
        self.max_tries = read.whole("SUBSYNC_MAX_TRIES", 1)  # subtitles tried per language and file
        self.cap_bytes = int(read.number("SUBSYNC_STORE_CAP_MB", scale=MB))
        self.daily_budget = int(read.number("SUBSYNC_DAILY_BUDGET_GB", scale=GB))
        self.backoff_s = read.whole("SUBSYNC_403_BACKOFF_S", 1)
        self.retry_stopped_s = read.whole("SUBSYNC_RETRY_STOPPED_S", 1)
        # the audio route: a file with no text or image subtitle track is timed from its speech, reading audio windows
        # within SUBSYNC_AUDIO_BUDGET_GB per file (shorter windows on high-bitrate files, none when they would be
        # too short); off, every file is read index only (SUBSYNC_INDEX_CAP_MB)
        self.audio_route = read.switch("SUBSYNC_AUDIO_ROUTE")
        self.audio_budget = int(read.number("SUBSYNC_AUDIO_BUDGET_GB", scale=GB)) or None
        self.file_cap = int(read.number("SUBSYNC_FILE_CAP_GB", scale=GB)) or None
        # when a file with no usable track starts its audio sync: pick, only when the viewer downloads subsync's
        # own entry for the file (proof of play: a plain subtitle request also comes for files nobody played, as
        # AIOMetadata asks for the first listed version); open, as soon as it is opened, within the checks of
        # Worker._may_open. In public mode each person's own choice replaces it (public.user_settings), "off" included
        self.audio_when = _audio_when(e)
        # Audio syncs one viewer may start a UTC day, picked or opened, and of those, opened
        self.user_audio_per_day = read.whole("SUBSYNC_USER_AUDIO_PER_DAY")
        self.user_opened_per_day = read.whole("SUBSYNC_USER_OPENED_PER_DAY")
        # a pick (the "(Subsync)" entry's download) waits this long for the file's result, like AutoSync; AIOMetadata
        # gives up on a subtitle after 15 s
        self.pick_wait = read.number("SUBSYNC_PICK_WAIT_MS", above_zero=True) / 1000
        # Every candidate judged, the best fit offered first (up to SUBSYNC_MAIN_ALTERNATIVES for the first language)
        self.best_fit = read.switch("SUBSYNC_BEST_FIT")
        self.alternatives = read.whole("SUBSYNC_MAIN_ALTERNATIVES", 1)
        self.skip_ai = read.switch("SUBSYNC_SKIP_AI")             # AI-made, machine-translated, forced: never tried
        # the plain track as the reference, not the SDH one
        self.prefer_plain = read.switch("SUBSYNC_PREFER_PLAIN_TRACK")
        # Sources subsync searches itself (extra.py): the keyless ones by default; SubDL,
        # SubSource and Wyzie only with a key (in public mode the service's keys, and never Wyzie); TheSubtitleDB's key
        # only raises its download limit
        self.extra_sources = [x.strip().lower() for x in g("SUBSYNC_EXTRA_SOURCES").split(",")
                              if x.strip()]
        self.subdl_key = g("SUBSYNC_SUBDL_KEY").strip()
        self.subsource_key = g("SUBSYNC_SUBSOURCE_KEY").strip()
        self.tsdb_key = g("SUBSYNC_TSDB_KEY").strip()
        self.wyzie_key = g("SUBSYNC_WYZIE_KEY").strip()
        self.source_timeout = read.number("SUBSYNC_SOURCE_TIMEOUT_S", above_zero=True)
        # a source the AIOStreams config already has as a subtitle addon is asked once, through the config
        self.skip_config_sources = read.switch("SUBSYNC_SKIP_CONFIG_SOURCES")
        self.osv3_url = g("SUBSYNC_OSV3_URL").rstrip("/")
        self.subsource_url = g("SUBSYNC_SUBSOURCE_URL").rstrip("/")
        self.tsdb_url = g("SUBSYNC_TSDB_URL").rstrip("/")
        self.cinemeta_url = g("SUBSYNC_CINEMETA_URL").rstrip("/")
        self.gestdown_url = g("SUBSYNC_GESTDOWN_URL").rstrip("/")
        self.subdl_url = g("SUBSYNC_SUBDL_URL").rstrip("/")
        self.subdl_download_url = g("SUBSYNC_SUBDL_DOWNLOAD_URL").rstrip("/")
        self.wyzie_url = g("SUBSYNC_WYZIE_URL").rstrip("/")
        self.osv3plus_url = g("SUBSYNC_OSV3PLUS_URL").rstrip("/")
        # a title's original language (origlang.py): looked up by its id, so subtitles are timed to the file's track in
        # it; TMDB with a key (the instance's, or in public mode each person's own), Wikidata always; an operator's
        # list overrides any lookup
        self.original_language = read.switch("SUBSYNC_ORIGINAL_LANGUAGE")
        self.tmdb_key = g("SUBSYNC_TMDB_KEY").strip()
        self.tmdb_personal = read.switch("SUBSYNC_TMDB_PERSONAL")
        self.original_languages, self.original_languages_ignored = _overrides(g("SUBSYNC_ORIGINAL_LANGUAGES"))
        self.tmdb_url = g("SUBSYNC_TMDB_URL").rstrip("/")
        self.wikidata_url = g("SUBSYNC_WIKIDATA_URL")
        self.kitsu_url = g("SUBSYNC_KITSU_URL").rstrip("/")
        self.anilist_url = g("SUBSYNC_ANILIST_URL")
        # best fit: originals checked (no alass) for up to this many candidates; alass only on SUBSYNC_MAX_TRIES of them
        self.gate_tries = read.whole("SUBSYNC_GATE_TRIES", 1)
        self.index_cap = int(read.number("SUBSYNC_INDEX_CAP_MB", scale=MB))
        self.audio_lead = read.number("SUBSYNC_AUDIO_LEAD_MS") / 1000
        # a "(synced)" entry for a file still being worked on, served as the original until the copy is ready
        self.pending_entry = read.switch("SUBSYNC_PENDING_ENTRY")
        self.pending_wait = read.number("SUBSYNC_PENDING_WAIT_MS", above_zero=True) / 1000
        self.url_rewrite = g("SUBSYNC_URL_REWRITE")
        self.read_timeout = read.number("SUBSYNC_READ_TIMEOUT_S", above_zero=True)
        self.size_slack = int(read.number("SUBSYNC_SIZE_SLACK_MB", scale=MB))  # listed sizes are rounded to 0.01 GiB
        self.alass = g("SUBSYNC_ALASS")
        self.ffmpeg = g("SUBSYNC_FFMPEG")
        self.snap = read.switch("SUBSYNC_SNAP")                # per-line snap after alass
        self.snap_within = read.number("SUBSYNC_SNAP_WITHIN")  # a snapped copy's bar for "within 200 ms"
        # the second way to pass: a re-timed copy clearly closer to the release's track than the original
        self.better = {"paired": read.number("SUBSYNC_BETTER_PAIRED"),
                       "median_ms": read.number("SUBSYNC_BETTER_MEDIAN_MS"),
                       "ratio": read.number("SUBSYNC_BETTER_RATIO"),
                       # against another language's track: the alignment's median error (relgate.aligned)
                       "cross_median_ms": read.number("SUBSYNC_BETTER_CROSS_MEDIAN_MS"),
                       "cross_paired": read.number("SUBSYNC_BETTER_CROSS_PAIRED")} \
            if read.switch("SUBSYNC_BETTER") else None
        # with no track in a subtitle's language, a track in one of these languages is its reference first
        self.fallback_reference = tuple(lang_list(g("SUBSYNC_FALLBACK_REFERENCE")))
        # the audio route's second way to pass: a re-timed copy clearly closer to the speech than the original
        self.audio_better = {"matched": read.whole("SUBSYNC_AUDIO_BETTER_MATCHED"),
                             "miss": read.number("SUBSYNC_AUDIO_BETTER_MISS"),
                             "median_ms": read.number("SUBSYNC_AUDIO_BETTER_MEDIAN_MS"),
                             "within": read.number("SUBSYNC_AUDIO_BETTER_WITHIN"),
                             "min_shift_s": read.number("SUBSYNC_AUDIO_BETTER_MIN_SHIFT_S")} \
            if read.switch("SUBSYNC_AUDIO_BETTER") else None
        # a line that splits a release line counts as paired
        self.gate_splits = read.switch("SUBSYNC_GATE_SPLITS")
        self.rank_by_release = read.switch("SUBSYNC_RANK_BY_RELEASE")  # subtitles made for this release tried first
        # a waiting file's other languages: shifted onto the main guess
        self.anchored_guess = read.switch("SUBSYNC_ANCHORED_GUESS")
        self.list_in_sync = read.switch("SUBSYNC_LIST_IN_SYNC")        # an in-sync original further down listed as is
        self.image_ref = read.switch("SUBSYNC_IMAGE_REF")              # image subtitles' cue times as the reference
        self.fast_path = read.switch("SUBSYNC_FAST_PATH")              # originals checked first, in-sync listed at once
        self.shift_path = read.switch("SUBSYNC_SHIFT_PATH")            # then one steady offset (shift.py), alass after
        # Lines paired by their words where timing hasn't settled a language (textpair.py; D1, D2)
        self.text_pairing = read.switch("SUBSYNC_TEXT_PAIRING")
        self.sections = read.switch("SUBSYNC_SECTIONS")                   # D4: other cuts fitted in sections
        self.audio_extra = read.whole("SUBSYNC_AUDIO_EXTRA_WINDOWS")      # D5: windows a close call may add
        self.parallel_languages = read.switch("SUBSYNC_PARALLEL_LANGUAGES")  # languages at the same time
        # a private path key for the routes that start work or show status (manifest, subtitles, status), so a published
        # port or a reverse-proxy route can't be used to make subsync read files; the synced files, /health and the
        # entries subsync lists stay open, each entry's address carrying a code made from the key (addon.entry_code).
        # And an optional path prefix (e.g. /subsync) that a reverse proxy forwards unchanged.
        self.addon_key = g("SUBSYNC_ADDON_KEY").strip().strip("/")
        base = g("SUBSYNC_BASE_PATH").strip().strip("/")
        self.base_path = "/" + base if base else ""
        # rows of these addons (comma-separated names) are never read (empty by default)
        self.skip_addons = tuple(n.strip() for n in g("SUBSYNC_SKIP_ADDONS").split(",") if n.strip())
        # public mode: one service for many people, each signed up with their own AIOStreams (public.py)
        self.public = read.switch("SUBSYNC_PUBLIC")
        self.secret_key = g("SUBSYNC_SECRET_KEY").strip()  # encrypts users' records; never logged
        self.invite = g("SUBSYNC_PUBLIC_INVITE").strip()
        self.max_users = read.whole("SUBSYNC_MAX_USERS")
        self.signups_per_hour = read.whole("SUBSYNC_SIGNUPS_PER_HOUR")  # per address
        self.user_files_per_day = read.whole("SUBSYNC_USER_FILES_PER_DAY")
        self.user_daily_bytes = int(read.number("SUBSYNC_USER_DAILY_MB", scale=MB))
        self.user_queue = read.whole("SUBSYNC_USER_QUEUE")
        # the most languages a person may choose on the sign-up and settings pages
        self.max_languages = _language_limit(g("SUBSYNC_MAX_LANGUAGES"))
        self.active_workers = read.whole("SUBSYNC_ACTIVE_WORKERS")
        self.worker_idle_s = read.number("SUBSYNC_WORKER_IDLE_MIN", above_zero=True, scale=60)
        self.user_keep_days = read.whole("SUBSYNC_USER_KEEP_DAYS")
        # what the pages say about the host's own setup: the Questions link, the service in front that sees
        # every request, how many days backups keep records; empty, words true of any host instead (0 days: empty)
        self.contact = g("SUBSYNC_CONTACT").strip()
        self.front_proxy = g("SUBSYNC_FRONT_PROXY").strip()
        self.backup_days = read.whole("SUBSYNC_BACKUP_DAYS") if g("SUBSYNC_BACKUP_DAYS").strip() else 0
        # each person's AIOStreams checked again, at most this often, while they use the service (recheck.py); 0: never
        self.recheck_hours = read.number("SUBSYNC_RECHECK_HOURS")
        # the web server's addresses; "gateway" is this container's default gateway, found at start (the address a
        # web server reaching subsync through a port Docker publishes comes from), so no fixed Docker network is needed
        proxies = [a.strip() for a in g("SUBSYNC_TRUSTED_PROXY").split(",") if a.strip()]
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
        self.max_connections = read.whole("SUBSYNC_MAX_CONNECTIONS", 1)
        # files worked on at once for everyone (each person's worker takes a place of one shared turn)
        self.syncs_at_once = _places(g("SUBSYNC_SYNCS_AT_ONCE"))
        # audio syncs at once for everyone (public mode's audio places, worker.audio_places): auto is half the places
        self.audio_at_once = _audio_at_once(g("SUBSYNC_AUDIO_AT_ONCE"))
        # Usenet: off by default. On: files are also looked for through a second config, the Usenet
        # config, when the sync config doesn't have them, at most SUBSYNC_USENET_LISTINGS_PER_DAY times a day (per
        # person in public mode, where "on" lets each person turn it on for themselves and their record holds the
        # Usenet config's link); a file with no usable track takes the audio route like a debrid one
        self.usenet = read.switch("SUBSYNC_USENET")
        self.usenet_listings_per_day = read.whole("SUBSYNC_USENET_LISTINGS_PER_DAY")
        self.usenet_manifest = g("SUBSYNC_USENET_MANIFEST")
        self.usenet_password = g("SUBSYNC_USENET_PASSWORD")
        self.written_wrong = read.wrong     # the number and on/off settings written wrong, as conflicts() says them

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
    def audio_choice(self):
        """When a file with no usable track starts its audio sync, for this viewer: off (the instance's audio route is
        off, or the person chose off), pick or open."""
        if not self.audio_route:
            return "off"
        return self.audio_when if self.audio_when in AUDIO_CHOICES else "pick"

    @property
    def keeps_passwords(self):
        """Public mode: whether a person may let AIOSubtitles keep their AIOStreams password. Only the check
        (recheck.py) uses a kept password, so not while SUBSYNC_RECHECK_HOURS turns it off."""
        return self.recheck_hours > 0

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

    @property
    def usenet_usable(self):
        """Whether files are also looked for through the Usenet config: Usenet on, the config's link and
        password set, and at least one search a day. The one rule for the start, the self-hosted service and each
        person's worker in public mode."""
        return bool(self.usenet and self.usenet_manifest and self.usenet_password and self.usenet_listings_per_day > 0)

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
        # a setup with Usenet only finds files and subtitles through its Usenet config alone (conflicts() says when
        # that config can't be used)
        usenet_only = bool(self.usenet and self.usenet_manifest)
        return [name for name, value in (("SUBSYNC_SOURCE_MANIFEST", self.source_manifest or usenet_only),
                                         ("SUBSYNC_SOURCE_PASSWORD", self.source_password or usenet_only),
                                         ("SUBSYNC_PUBLIC_BASE", self.public_base)) if not value]

    def conflicts(self):
        """Settings written wrong or that can't go together, in plain sentences (empty: none)."""
        out = list(self.written_wrong)
        if self.audio_when is None:
            out.append("SUBSYNC_AUDIO_WHEN must be pick or open.")
        if self.audio_at_once is None:
            out.append("SUBSYNC_AUDIO_AT_ONCE must be a whole number from 1, or auto.")
        elif self.public and self.audio_at_once != "auto" and self.syncs_at_once \
                and self.audio_at_once > self.syncs_at_once:
            # a self-hosted install has one audio lane whatever it says, so only public mode's places are checked
            out.append(f"SUBSYNC_AUDIO_AT_ONCE can't be more than SUBSYNC_SYNCS_AT_ONCE ({self.syncs_at_once}).")
        if self.public and self.wyzie_key:
            out.append("Wyzie Subs isn't used in public mode (one free key per network, paused when used from servers): "
                       "remove SUBSYNC_WYZIE_KEY or turn public mode off.")
        if self.public and self.tmdb_key and self.tmdb_personal:
            out.append("SUBSYNC_TMDB_KEY and SUBSYNC_TMDB_PERSONAL can't both be set: set a key for everyone, or let "
                       "each person add their own.")
        if self.syncs_at_once is None:
            out.append("SUBSYNC_SYNCS_AT_ONCE must be a whole number from 1, or auto.")
        if self.max_languages is None:
            out.append(f"SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to {MAX_LANGUAGES}.")
        if self.public and self.gateway_missing:
            out.append("SUBSYNC_TRUSTED_PROXY says gateway, but this machine has no default gateway to trust: give your "
                       "web server's address instead.")
        if not self.public:     # a public instance uses each person's own languages and configs, and no addon key
            out += self._own_conflicts()
        return out

    def _own_conflicts(self):
        """A self-hosted install's languages, addon key and Usenet config, in plain sentences."""
        out = []
        if not self.languages:
            out.append("SUBSYNC_LANGUAGES must name at least one language, like eng.")
        out += [f"SUBSYNC_LANGUAGES must be language codes separated by commas, like eng,rus: {code} isn't one."
                for code in self.unknown_languages]
        if "/" in self.addon_key:          # the key is one part of an address
            out.append("SUBSYNC_ADDON_KEY can't contain /.")
        if self.usenet and self.usenet_manifest and not self.usenet_usable:
            if not self.usenet_password:
                out.append("SUBSYNC_USENET_MANIFEST needs its password: set SUBSYNC_USENET_PASSWORD (./setup writes "
                           "both).")
            if not self.source_manifest and self.usenet_listings_per_day < 1:
                out.append("Usenet is this install's only source, so SUBSYNC_USENET_LISTINGS_PER_DAY must be a whole "
                           "number from 1.")
        return out
