"""Subtitle sources subsync searches itself, besides what the user's AIOStreams lists: TheSubtitleDB,
OpenSubtitles V3+ and, for series, Gestdown, which need no key; SubDL and Wyzie when the user set a key. A subtitle made from the very release
(a rip of its own track) is often only found there, and only such a subtitle fits a file as closely as its own track.

Each source is asked once per file and language list; a source that fails or is slow (SUBSYNC_SOURCE_TIMEOUT_S) is
left out, the others still count. Every candidate says what its source knows: release name, AI-made or machine-translated
(`ai`), hearing impaired (`hi`) and, from TheSubtitleDB, its line count (`cues`), which ranks a likely rip early.
OpenSubtitles V3+ is asked as a player asks it, with the file's hash, size and name (its `moviehash` marks a
subtitle made for that very file). No key or link is ever logged.
"""
import json
import logging
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .langs import lang3, name as lang_name
from .sources import extras, release_info

log = logging.getLogger("subsync.extra")
USER_AGENT = "AIOSubtitles/1.4 (subsync)"
TSDB_PAGES = 5                     # at most 5 pages of 20: the likeliest rips are ranked by line count anyway
TWO_LETTER = {"eng": "en", "rus": "ru", "jpn": "ja", "fre": "fr", "ger": "de", "spa": "es", "ita": "it", "por": "pt",
              "kor": "ko", "chi": "zh", "ukr": "uk", "pol": "pl", "dut": "nl", "swe": "sv", "tur": "tr", "ara": "ar",
              "heb": "he", "cze": "cs", "hun": "hu", "gre": "el", "fin": "fi", "dan": "da", "nor": "no", "rum": "ro",
              "bul": "bg", "hrv": "hr", "srp": "sr", "ind": "id", "tha": "th", "vie": "vi", "hin": "hi", "per": "fa"}


def two(code):
    return TWO_LETTER.get(lang3(code), lang3(code)[:2])


def split_id(video_id):
    """'tt1:2:3' -> ('tt1', 2, 3); 'tt1' -> ('tt1', None, None)."""
    parts = str(video_id).split(":")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0], int(parts[1]), int(parts[2])
    return parts[0], None, None


class ExtraSources:
    def __init__(self, settings):
        self.s = settings
        self.shows = {}             # tvdb id -> Gestdown show id (or None), kept for the process
        self.tvdb = {}              # imdb id -> tvdb id

    def _get(self, url, timeout=None):
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout or self.s.source_timeout) as r:
            return json.loads(r.read(4 * 1024 * 1024) or b"null")

    def search(self, type_, video_id, langs=None, hints=None):
        """Candidates from every configured source, each a dict like sources.Aios.subtitles gives, plus `source`.
        `hints` are the played file's videoHash, videoSize and filename, as a player sends them."""
        langs = [lang3(x) for x in (langs or self.s.languages)]
        hints = hints or {}
        jobs = []
        if "tsdb" in self.s.extra_sources:
            jobs.append(("tsdb", self._tsdb))
        if "osv3plus" in self.s.extra_sources:
            jobs.append(("osv3plus", lambda t, v, l: self._osv3plus(t, v, l, hints)))
        if "gestdown" in self.s.extra_sources and type_ == "series":
            jobs.append(("gestdown", self._gestdown))
        if self.s.subdl_key:
            jobs.append(("subdl", self._subdl))
        if self.s.wyzie_key:
            jobs.append(("wyzie", self._wyzie))
        if not jobs:
            return []
        out = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="subsync-extra") as pool:
            futures = {pool.submit(fn, type_, video_id, langs): src for src, fn in jobs}
            for fut, src in futures.items():
                try:
                    got = fut.result()
                except Exception as e:  # a source that fails is left out
                    log.info("source %s failed for %s/%s: %s", src, type_, video_id, type(e).__name__)
                    continue
                out += [c for c in got if c["lang"] in langs]
        log.info("extra sources %s/%s: %d subtitles (%s) in %d ms", type_, video_id, len(out),
                 ", ".join(f"{src} {sum(c['source'] == src for c in out)}" for src, _ in jobs), (time.time() - t0) * 1000)
        return out

    # -- OpenSubtitles V3+ -----------------------------------------------------------------------------
    def _osv3plus(self, type_, video_id, langs, hints):
        """The addon AIOStreams' "OpenSubtitles V3+" preset adds, asked with the same path: languages, then options
        (AI-translated ones only with SUBSYNC_SKIP_AI off; every uploader; no server-side timing change)."""
        opts = f"ai-translated={'false' if self.s.skip_ai else 'true'}|from=all|auto-adjustment=false"
        config = urllib.parse.quote("|".join(two(x) for x in langs), safe="") + "/" + urllib.parse.quote(opts, safe="")
        ex = extras(hints)
        got = self._get(f"{self.s.osv3plus_url}/{config}/subtitles/{urllib.parse.quote(type_)}/"
                        f"{urllib.parse.quote(video_id, safe=':')}{'/' + ex if ex else ''}.json") or {}
        out = []
        for it in got.get("subtitles") or []:
            if not isinstance(it.get("url"), str) or not it["url"]:
                continue
            out.append({"id": str(it.get("id") or it["url"]), "source": "osv3plus", "url": it["url"],
                        "lang": lang3(it.get("lang")), **release_info(it)})
        return out

    # -- TheSubtitleDB ---------------------------------------------------------------------------------------
    def _tsdb(self, type_, video_id, langs):
        imdb, season, episode = split_id(video_id)
        path = f"/v1/by-imdb/{imdb}" + (f"/season/{season}/episode/{episode}" if season is not None else "")
        out, offset = [], 0
        for _ in range(TSDB_PAGES):
            q = urllib.parse.urlencode({"lang": ",".join(two(x) for x in langs), "offset": offset})
            page = (self._get(f"{self.s.tsdb_url}{path}?{q}") or {}).get("subtitles") or {}
            items = page.get("items") or []
            for it in items:
                if not it.get("download_url") and it.get("id") is None:
                    continue
                out.append({"id": f"tsdb-{it.get('id')}", "source": "tsdb", "lang": lang3(it.get("language")),
                            "url": it.get("download_url") or f"{self.s.tsdb_url}/get/{it.get('id')}",
                            "release": str(it.get("release_name") or ""), "hash": "", "hash_match": False,
                            "ai": bool(it.get("ai") or it.get("ai_transcribed")), "hi": bool(it.get("hearing_impaired")),
                            "cues": it.get("cues") if isinstance(it.get("cues"), int) else None})
            offset += len(items)
            if not items or offset >= int(page.get("total") or 0):
                break
        return out

    # -- Gestdown (series) -------------------------------------------------------------------------------------
    def _gestdown(self, type_, video_id, langs):
        imdb, season, episode = split_id(video_id)
        if season is None:
            return []
        if imdb not in self.tvdb:
            meta = (self._get(f"{self.s.cinemeta_url}/meta/series/{imdb}.json") or {}).get("meta") or {}
            self.tvdb[imdb] = meta.get("tvdb_id")
        tvdb = self.tvdb[imdb]
        if not tvdb:
            return []
        if tvdb not in self.shows:
            shows = (self._get(f"{self.s.gestdown_url}/shows/external/tvdb/{tvdb}") or {}).get("shows") or []
            self.shows[tvdb] = shows[0]["id"] if shows else None
        show = self.shows[tvdb]
        if not show:
            return []
        out = []
        for lang in langs:
            got = self._get(f"{self.s.gestdown_url}/subtitles/get/{show}/{season}/{episode}/"
                            f"{urllib.parse.quote(lang_name(lang))}") or {}
            for it in got.get("matchingSubtitles") or []:
                if not it.get("completed"):
                    continue
                out.append({"id": f"gd-{it.get('subtitleId')}", "source": "gestdown", "lang": lang,
                            "url": f"{self.s.gestdown_url}{it.get('downloadUri')}", "release": str(it.get("version") or ""),
                            "hash": "", "hash_match": False, "ai": False, "hi": bool(it.get("hearingImpaired"))})
        return out

    # -- SubDL (key) -------------------------------------------------------------------------------------------
    def _subdl(self, type_, video_id, langs):
        imdb, season, episode = split_id(video_id)
        q = {"api_key": self.s.subdl_key, "imdb_id": imdb, "type": "tv" if season is not None else "movie",
             "languages": ",".join(two(x).upper() for x in langs), "subs_per_page": 30}
        if season is not None:
            q.update(season_number=season, episode_number=episode)
        got = self._get(f"{self.s.subdl_url}/api/v1/subtitles?{urllib.parse.urlencode(q)}") or {}
        out = []
        for it in got.get("subtitles") or []:
            url = it.get("url") or ""
            if not url:
                continue
            frag = f"#S{season:02d}E{episode:02d}" if season is not None else ""   # which file of a zip to take
            out.append({"id": f"subdl-{url.rsplit('/', 1)[-1]}", "source": "subdl",
                        "lang": lang3(it.get("language") or it.get("lang")),
                        "url": f"{self.s.subdl_download_url}{url}{frag}", "release": str(it.get("release_name") or ""),
                        "hash": "", "hash_match": False, "ai": False, "hi": bool(it.get("hi"))})
        return out

    # -- Wyzie (key) -------------------------------------------------------------------------------------------
    def _wyzie(self, type_, video_id, langs):
        imdb, season, episode = split_id(video_id)
        out = []
        for lang in langs:
            q = {"id": imdb, "language": two(lang), "key": self.s.wyzie_key}
            if season is not None:
                q.update(season=season, episode=episode)
            got = self._get(f"{self.s.wyzie_url}/search?{urllib.parse.urlencode(q)}") or []
            for it in got if isinstance(got, list) else []:
                if not it.get("url"):
                    continue
                out.append({"id": f"wyzie-{it.get('id')}", "source": "wyzie", "lang": lang3(it.get("language") or lang),
                            "url": it["url"], "release": str(it.get("release") or it.get("display") or ""),
                            "hash": "", "hash_match": False, "ai": False, "hi": bool(it.get("isHearingImpaired"))})
        return out
