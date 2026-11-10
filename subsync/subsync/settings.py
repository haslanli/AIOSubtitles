"""Settings from the environment. The source config's address and the addon key are secrets and are never logged."""
import os

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
        self.languages = lang_list(g("SUBSYNC_LANGUAGES", "eng"))
        self.max_tries = int(g("SUBSYNC_MAX_TRIES", "6"))                      # subtitles tried per language and file
        self.cap_bytes = int(float(g("SUBSYNC_STORE_CAP_MB", "2048")) * 1024 * 1024)
        self.daily_budget = int(float(g("SUBSYNC_DAILY_BUDGET_GB", "50")) * 1e9)
        self.backoff_s = int(g("SUBSYNC_403_BACKOFF_S", "900"))
        self.retry_stopped_s = int(g("SUBSYNC_RETRY_STOPPED_S", "600"))
        # the audio route: a file with no text or image subtitle track is timed from its speech, reading 0.3-3 GB of
        # audio windows (at most SUBSYNC_FILE_CAP_GB); off, every file is read index only (SUBSYNC_INDEX_CAP_MB)
        self.audio_route = _on(g("SUBSYNC_AUDIO_ROUTE", "off"))
        self.file_cap = int(float(g("SUBSYNC_FILE_CAP_GB", "6")) * 1e9) or None
        # the audio route starts only when the viewer downloads subsync's own entry for the file (proof of play): a plain
        # subtitle request also comes for files nobody played (AIOMetadata asks for the first listed version). Off: the
        # audio route starts on any request, as before.
        self.audio_on_pick = _on(g("SUBSYNC_AUDIO_ON_PICK", "on"))
        self.pick_wait = float(g("SUBSYNC_PICK_WAIT_MS", "4000")) / 1000   # a pick waits this long for the subtitle list
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
                       "ratio": float(g("SUBSYNC_BETTER_RATIO", "0.5"))} if _on(g("SUBSYNC_BETTER", "on")) else None
        self.gate_splits = _on(g("SUBSYNC_GATE_SPLITS", "on"))               # a line that splits a release line counts as paired
        self.rank_by_release = _on(g("SUBSYNC_RANK_BY_RELEASE", "on"))       # subtitles made for this release tried first
        self.list_in_sync = _on(g("SUBSYNC_LIST_IN_SYNC", "on"))             # an in-sync original further down listed as is
        self.image_ref = _on(g("SUBSYNC_IMAGE_REF", "on"))                   # image subtitles' cue times as the reference
        self.fast_path = _on(g("SUBSYNC_FAST_PATH", "on"))                   # originals checked first, in-sync listed at once
        self.parallel_languages = _on(g("SUBSYNC_PARALLEL_LANGUAGES", "on")) # languages at the same time
        # a private path key for the routes that start work or show status (manifest, subtitles, status), so a published
        # port or a reverse-proxy route can't be used to make subsync read files; the synced files and /health stay
        # open. And an optional path prefix (e.g. /subsync) that a reverse proxy forwards unchanged.
        self.addon_key = g("SUBSYNC_ADDON_KEY", "").strip().strip("/")
        base = g("SUBSYNC_BASE_PATH", "").strip().strip("/")
        self.base_path = "/" + base if base else ""
        # rows of these addons (comma-separated names) are never read (empty by default)
        self.skip_addons = tuple(n.strip() for n in g("SUBSYNC_SKIP_ADDONS", "").split(",") if n.strip())

    def missing(self):
        """What must be set before the service can work."""
        return [name for name, value in (("SUBSYNC_SOURCE_MANIFEST", self.source_manifest),
                                         ("SUBSYNC_PUBLIC_BASE", self.public_base)) if not value]
