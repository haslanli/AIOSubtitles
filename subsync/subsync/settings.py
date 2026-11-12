"""Settings from the environment. The source config's address and the addon key are secrets and are never logged."""
import base64
import binascii
import hashlib
import json
import os

from . import ALGO_VERSION, RULE_REVISION
from .worker import lang_list


def _on(value):
    return str(value).strip().lower() in ("1", "on", "true", "yes")


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
        self.user_files_per_day = int(g("SUBSYNC_USER_FILES_PER_DAY", "40"))
        self.user_daily_bytes = int(float(g("SUBSYNC_USER_DAILY_MB", "500")) * 1024 * 1024)
        self.user_queue = int(g("SUBSYNC_USER_QUEUE", "3"))
        self.active_workers = int(g("SUBSYNC_ACTIVE_WORKERS", "30"))
        self.worker_idle_s = float(g("SUBSYNC_WORKER_IDLE_MIN", "30")) * 60
        self.user_keep_days = int(g("SUBSYNC_USER_KEEP_DAYS", "180"))
        self.trusted_proxy = tuple(a.strip() for a in g("SUBSYNC_TRUSTED_PROXY", "127.0.0.1").split(",") if a.strip())
        self.max_connections = int(g("SUBSYNC_MAX_CONNECTIONS", "64"))

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
                "shift_path": self.shift_path, "fallback_reference": self.fallback_reference}
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
        return [name for name, value in (("SUBSYNC_SOURCE_MANIFEST", self.source_manifest),
                                         ("SUBSYNC_SOURCE_PASSWORD", self.source_password),
                                         ("SUBSYNC_PUBLIC_BASE", self.public_base)) if not value]

    def conflicts(self):
        """Settings that can't go together, in plain sentences (empty: none)."""
        out = []
        if self.public and self.audio_route:
            out.append("The audio route is off in public mode: set SUBSYNC_AUDIO_ROUTE=off or turn public mode off.")
        if self.public and self.wyzie_key:
            out.append("Wyzie Subs isn't used in public mode (one free key per network, paused when used from servers): "
                       "remove SUBSYNC_WYZIE_KEY or turn public mode off.")
        return out
