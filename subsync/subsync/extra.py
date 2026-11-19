"""Subtitle sources AIOSubtitles asks itself, besides what the user's AIOStreams lists:
Stremio's OpenSubtitles v3 and OpenSubtitles V3+ (asked like a player, with the file's hash, size and name),
TheSubtitleDB (by IMDb id, by the file's torrent hash and by its release name) and, for series, Gestdown, which need no
key; SubDL, SubSource and Wyzie with a key. A subtitle made from the very release (a rip of its own track) is often only
found there, and only such a subtitle fits a file as closely as its own track.

All sources are asked at once. Each answer must come within SUBSYNC_SOURCE_TIMEOUT_S; a later answer is still kept
(srclimits.SEARCH_TTL_S, "nothing found" EMPTY_TTL_S), so the next play of the title has it. A source that keeps failing
is paused (sourcestate.Health); a site's own limits are counted (sourcestate.Ledger). A source the user's config already
has is skipped (`skip`): its answers come through the config. Only an IMDb id is sent as one; for other ids (Kitsu,
MAL) only the lookups that need no id are made. No key or link is ever logged or put in a subtitle's address.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

from . import choose, digits, net, realdebrid, srclimits
from .langs import lang2, lang3, name as lang_name
from .sourcestate import Health, Ledger
from .sources import extras, release_info, release_name, resource_path
from .version import user_agent

log = logging.getLogger("subsync.extra")
USER_AGENT = user_agent("subsync")
# SubSource names its languages in words
SUBSOURCE_LANGS = {"eng": "english", "rus": "russian", "jpn": "japanese", "fre": "french", "ger": "german",
                   "spa": "spanish", "ita": "italian", "por": "portuguese", "kor": "korean", "chi": "chinese",
                   "ukr": "ukrainian", "pol": "polish", "dut": "dutch", "swe": "swedish", "tur": "turkish",
                   "ara": "arabic", "heb": "hebrew", "cze": "czech", "hun": "hungarian", "gre": "greek",
                   "fin": "finnish", "dan": "danish", "nor": "norwegian", "rum": "romanian", "bul": "bulgarian",
                   "hrv": "croatian", "srp": "serbian", "ind": "indonesian", "tha": "thai", "vie": "vietnamese",
                   "hin": "hindi", "per": "farsi_persian"}
SUBSOURCE_BY_NAME = {v: k for k, v in SUBSOURCE_LANGS.items()}
MACHINE = re.compile(r"(?i)machine|\bai\b|auto[ ._-]?translat")


def two_letter(langs):
    """The ISO 639-1 codes of languages, for the sources that ask by them; a language ISO 639-1 has no code for is left
    out (it is never asked as another one)."""
    return [code for code in map(lang2, langs) if code]


def split_id(video_id):
    """'tt1:2:3' -> ('tt1', 2, 3); 'tt1' -> ('tt1', None, None). Any other id (Kitsu, MAL, TMDB) gives no IMDb id
    and no season or episode: only the lookups that need no id can be made for it."""
    parts = str(video_id).split(":")
    if not re.fullmatch(r"tt[0-9]+", parts[0]):        # ASCII digits only, as every number of a request
        return None, None, None
    season, episode = (digits.whole(parts[1]), digits.whole(parts[2])) if len(parts) >= 3 else (None, None)
    if season is not None and episode is not None:
        return parts[0], season, episode
    return parts[0], None, None


def other_episode(release, season, episode):
    """Does a release name name episodes, none of them (season, episode)? A name without an episode tag (a season pack,
    a film) doesn't (choose's parsing: S01E05_, S01E05E06, S01E01-E10)."""
    if season is None or episode is None:
        return False
    return bool(choose.episodes(release)) and choose.other_episode(release, (season, episode))


def asked_sources(settings, type_):
    """The sources an instance asks itself for this kind of title ("movie" or "series"), in srclimits.SOURCE_ORDER: the
    keyless ones it lists (SUBSYNC_EXTRA_SOURCES), SubDL and SubSource with their key, Wyzie with its key on one's own
    install; Gestdown for series only."""
    on = set(settings.extra_sources)
    if settings.subdl_key:
        on.add("subdl")
    if settings.subsource_key:
        on.add("subsource")
    if settings.wyzie_key and not settings.public:
        on.add("wyzie")
    if type_ != "series":
        on.discard("gestdown")
    return [s for s in srclimits.SOURCE_ORDER if s in on]


def cleanable_presets(settings):
    """The subtitle addons the sign-up's clean-up takes out of a config, in srclimits.CLEANABLE_PRESETS' order:
    only those whose site the instance asks itself, so a person keeps every source it doesn't."""
    asked = set(asked_sources(settings, "movie")) | set(asked_sources(settings, "series"))
    return tuple(p for p in srclimits.CLEANABLE_PRESETS if srclimits.CONFIG_PRESETS[p] in asked)


class NotFound(Exception):
    """The source has nothing for this title (404): an answer, not a failure."""


class Busy(Exception):
    """The source asked to be asked again later without being down (Gestdown's 423), or a site it needs an id from did
    (Cinemeta's "too many requests"): nothing now, nothing kept, no pause."""


class Limited(Exception):
    def __init__(self, retry_after=None):
        super().__init__("too many requests")
        self.retry_after = retry_after


class ExtraSources:
    def __init__(self, settings, ledger=None, health=None, clock=time.time):
        self.s = settings
        self.clock = clock
        self.health = health or Health(clock)
        self.ledger = ledger
        self.cache = {}             # (source, type, id, langs, keys) -> (time, candidates)
        self.ids = {}               # (kind, key) -> (time, id or None): TVDB ids, Gestdown shows, SubSource ids
        self.cache_lock = threading.Lock()      # the pool's threads share cache and ids
        self.pool = ThreadPoolExecutor(max_workers=srclimits.CALLS_AT_ONCE, thread_name_prefix="subsync-extra")
        self.asking = threading.local()        # per pool thread: the requests made of the source it asks (_ask)

    # -- asking ------------------------------------------------------------------------------------------------
    def _get(self, url, headers=None, timeout=None, lookup=False):
        """A source's JSON answer. 404 is NotFound, 423 Busy, and "too many requests" (429, or 503 with Retry-After:
        sourcestate's rule) Limited; any other error is raised as it is. `lookup`: a request to another site for an id
        the source needs (Cinemeta's TVDB id), not a request of the source itself."""
        if not lookup:
            self.asking.requests = getattr(self.asking, "requests", 0) + 1
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": USER_AGENT,
                                                   **(headers or {})})
        try:
            with realdebrid.urlopen(req, timeout=timeout or max(self.s.source_timeout, srclimits.MIN_TIMEOUT_S)) as r:
                return json.loads(r.read(srclimits.ANSWER_MAX_BYTES) or b"null")
        except urllib.error.HTTPError as e:
            code, retry = e.code, e.headers.get("Retry-After") if e.headers else None
            e.close()
            if code == 404:
                raise NotFound() from None
            if code == 423:
                raise Busy() from None
            if code == 429 or (code == 503 and retry):
                raise Limited(retry) from None
            raise

    def _id(self, kind, key, lookup):
        """An id another site gives for a title, kept for the process; "none" is asked again after EMPTY_TTL_S. The
        table is shared by the pool's threads (cache_lock); the lookup itself runs outside the lock."""
        with self.cache_lock:
            got = self.ids.get((kind, key))
        if got and (got[1] is not None or self.clock() - got[0] < srclimits.EMPTY_TTL_S):
            return got[1]
        value = lookup()
        with self.cache_lock:
            self.ids[(kind, key)] = (self.clock(), value)
            while len(self.ids) > srclimits.SEARCH_CACHE_ENTRIES:
                self.ids.pop(next(iter(self.ids)))
        return value

    def _count(self, source, kind="request", n=1):
        if self.ledger is not None:
            self.ledger.count(source, kind, n)

    def headers_for(self, cand):
        """The headers a candidate's download needs (a source's key), never part of its address; only for an address
        on the source's own origin (the scheme, host and port of its configured address), so a key goes neither to
        another site nor over http to an https source."""
        source, url = cand.get("source"), cand.get("url") or ""
        if source == "subsource" and self.s.subsource_key and net.same_origin(url, self.s.subsource_url):
            return {"X-API-Key": self.s.subsource_key}
        if source == "tsdb" and self.s.tsdb_key and net.same_origin(url, self.s.tsdb_url):
            return {"Authorization": f"Bearer {self.s.tsdb_key}"}
        return {}

    def sources(self, type_):
        """The sources asked for this kind of title, in srclimits.SOURCE_ORDER (asked_sources)."""
        return asked_sources(self.s, type_)

    def search(self, type_, video_id, langs=None, hints=None, skip=()):
        """Candidates from every source asked, each a dict like sources.Aios.subtitles gives plus `source` and
        `release_match`. `hints`: the played file's videoHash, videoSize and filename as a player sends them, and its
        torrent's infoHash and fileIdx from the listing."""
        langs = [lang3(x) for x in (langs or self.s.languages)]
        hints = hints or {}
        fns = {"osv3": self._osv3, "osv3plus": self._osv3plus, "tsdb": self._tsdb, "gestdown": self._gestdown,
               "subdl": self._subdl, "subsource": self._subsource, "wyzie": self._wyzie}
        cache_hints = (hints.get("videoHash"), hints.get("videoSize"), hints.get("filename"), hints.get("infoHash"),
                       hints.get("fileIdx"))
        now = self.clock()
        answers, futures = {}, {}
        for source in self.sources(type_):
            if source in skip:
                continue
            key = (source, type_, str(video_id), tuple(langs), cache_hints)
            with self.cache_lock:
                got = self.cache.get(key)
            if got and now - got[0] < (srclimits.SEARCH_TTL_S if got[1] else srclimits.EMPTY_TTL_S):
                answers[source] = got[1]
                continue
            if source in srclimits.KEYED and self.ledger is not None \
                    and self.ledger.exhausted(source, "request"):
                continue
            if not self.health.allowed(source):
                continue
            futures[self.pool.submit(self._ask, source, fns[source], key, type_, video_id, langs, hints)] = source
        t0 = time.time()
        if futures:
            done, late = wait(futures, timeout=self.s.source_timeout)
            for fut in done:
                answers[futures[fut]] = fut.result()
            for fut in late:   # its answer is kept when it comes (in _ask), for the next play
                log.info("source %s too slow for %s/%s: left out this time", futures[fut], type_, video_id)
        out = []
        for source in self.sources(type_):
            out += [c for c in answers.get(source) or [] if c["lang"] in langs]
        log.info("extra sources %s/%s: %d subtitles (%s) in %d ms", type_, video_id, len(out),
                 ", ".join(f"{src} {sum(c['source'] == src for c in out)}" for src in answers), (time.time() - t0) * 1000)
        return out

    def _ask(self, source, fn, key, type_, video_id, langs, hints):
        """One source's answer (a list; empty when it failed), kept in the cache unless it failed or was busy."""
        self.asking.requests = 0
        try:
            got = fn(type_, video_id, langs, hints)
        except NotFound:
            got = []
        except Busy:
            self._answered(source)
            log.info("source %s busy for %s/%s: asked again next time", source, type_, video_id)
            return []
        except Limited as e:
            self.health.limited(source, e.retry_after)
            return []
        except urllib.error.HTTPError as e:
            self.health.failed(source, f"HTTP {e.code}")
            log.info("source %s failed for %s/%s: HTTP %d", source, type_, video_id, e.code)
            return []
        except Exception as e:  # a source that fails is left out; the others still count
            self.health.failed(source, type(e).__name__)
            log.info("source %s failed for %s/%s: %s", source, type_, video_id, type(e).__name__)
            return []
        self._answered(source)
        seen, unique = set(), []
        for c in got:          # the same subtitle found twice by one source (TheSubtitleDB's three lookups) counts once
            if (c["id"], c["lang"]) not in seen:
                seen.add((c["id"], c["lang"]))
                unique.append(c)
        with self.cache_lock:
            self.cache[key] = (self.clock(), unique)
            while len(self.cache) > srclimits.SEARCH_CACHE_ENTRIES:
                self.cache.pop(next(iter(self.cache)))
        return unique

    def _answered(self, source):
        """The source answered: it is open again (Health.ok) when it was asked at all; a call that asked it nothing (an
        id it can't use, nothing to look it up by) only gives back a pause's trial (Health.not_asked)."""
        if getattr(self.asking, "requests", 0):
            self.health.ok(source)
        else:
            self.health.not_asked(source)

    @staticmethod
    def _cand(source, cid, url, lang, release="", **flags):
        return {"id": cid, "source": source, "url": url, "lang": lang, "release": str(release or ""), "hash": "",
                "hash_match": bool(flags.pop("hash_match", False)), "release_match": bool(flags.pop("release_match", False)),
                "ai": bool(flags.pop("ai", False)), "hi": bool(flags.pop("hi", False)), **flags}

    # -- Stremio's OpenSubtitles v3 ----------------------------------------------------------------------
    def _osv3(self, type_, video_id, langs, hints):
        """Stremio's own OpenSubtitles addon, asked as a player asks it; it lists every language. `m: "h"` marks a
        subtitle found by the file's hash."""
        imdb, _, _ = split_id(video_id)
        if not imdb:
            return []
        ex = extras(hints)
        got = self._get(f"{self.s.osv3_url}/subtitles/{resource_path(type_, video_id)}"
                        f"{'/' + ex if ex else ''}.json") or {}
        out = []
        for it in got.get("subtitles") or []:
            if not isinstance(it.get("url"), str) or not it["url"]:
                continue
            out.append(dict(self._cand("osv3", f"osv3-{it.get('id') or it['url']}", it["url"], lang3(it.get("lang"))),
                            **release_info(it)))
        return out

    # -- OpenSubtitles V3+ -----------------------------------------------------------------------------
    def _osv3plus(self, type_, video_id, langs, hints):
        """The addon AIOStreams' "OpenSubtitles V3+" preset adds, asked with the same path: languages, then options
        (AI-translated ones only with SUBSYNC_SKIP_AI off; every uploader; no server-side timing change)."""
        imdb, _, _ = split_id(video_id)
        codes = two_letter(langs)
        if not imdb or not codes:
            return []
        opts = f"ai-translated={'false' if self.s.skip_ai else 'true'}|from=all|auto-adjustment=false"
        config = urllib.parse.quote("|".join(codes), safe="") + "/" + urllib.parse.quote(opts, safe="")
        ex = extras(hints)
        got = self._get(f"{self.s.osv3plus_url}/{config}/subtitles/{resource_path(type_, video_id)}"
                        f"{'/' + ex if ex else ''}.json") or {}
        out = []
        for it in got.get("subtitles") or []:
            if not isinstance(it.get("url"), str) or not it["url"]:
                continue
            out.append({"id": str(it.get("id") or it["url"]), "source": "osv3plus", "url": it["url"],
                        "lang": lang3(it.get("lang")), "release_match": False, **release_info(it)})
        return out

    # -- TheSubtitleDB -----------------------------------------------------------------------------------------
    def _tsdb(self, type_, video_id, langs, hints):
        """By the torrent's hash first (subtitles for that very video), then by release name, then by IMDb id; a
        subtitle found twice counts once. Lookups have no limit; downloads are counted by the worker.
        A release-name lookup answers with the closest title TheSubtitleDB has, which can be another film (an
        Inception file name got Nomadland, score 0.57): its subtitles count only when that title is the played one,
        and only its matched subtitle, at a score of at least srclimits.TSDB_RELEASE_SCORE, is a release match; with
        no IMDb id to check the title against, only that matched subtitle counts."""
        imdb, season, episode = split_id(video_id)
        codes = two_letter(langs)
        if not codes:
            return []
        name = release_name(hints.get("filename"))
        if season is None and name:
            tags = choose.episodes(name)
            if tags:
                season, episode = tags[0][0], tags[0][1]
        headers = {"Authorization": f"Bearer {self.s.tsdb_key}"} if self.s.tsdb_key else {}
        query = {"lang": ",".join(codes), "limit": srclimits.TSDB_PAGE_SIZE}
        if self.s.skip_ai:
            query["ai"] = "exclude"
        out, errors, tried = [], [], 0

        def take(path, params, pages=1, release=False, hash_match=False):
            """One lookup; its error is logged and leaves the other lookups' subtitles (a 429 pauses the source)."""
            nonlocal tried
            tried += 1
            try:
                pages_of(path, params, pages, release, hash_match)
            except Limited:
                raise
            except Exception as e:
                errors.append(e)
                log.info("source tsdb %s/%s %s: %s", type_, video_id, path.split("/")[2],
                         f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__)

        def same_title(answer):
            got = str(((answer.get("title") or {}).get("imdb")) or "")
            return not imdb or not got or got.lstrip("t") == imdb.lstrip("t")

        def pages_of(path, params, pages, release, hash_match):
            offset = 0
            for _ in range(pages):
                try:
                    answer = self._get(f"{self.s.tsdb_url}{path}?{urllib.parse.urlencode(dict(params, offset=offset))}",
                                       headers) or {}
                except NotFound:
                    return
                if not same_title(answer):
                    return
                page = answer.get("subtitles") or {}
                match = answer.get("match") or {}
                strong = isinstance(match.get("score"), (int, float)) and match["score"] >= srclimits.TSDB_RELEASE_SCORE
                items = page.get("items") or []
                # a torrent's subtitles that name other episodes make it a batch: then only those naming the played
                # episode are its own, and the ones naming none are ordinary candidates
                batch = hash_match and any(other_episode(it.get("release_name"), season, episode) for it in items)
                for it in items:
                    if it.get("id") is None or other_episode(it.get("release_name"), season, episode):
                        continue
                    matched = release and strong and it.get("id") == match.get("sub_id")
                    if release and not imdb and not matched:
                        continue   # the title can't be checked: only its matched subtitle counts
                    own = hash_match and (not batch or bool(choose.episodes(it.get("release_name"))))
                    out.append(self._cand("tsdb", f"tsdb-{it.get('id')}",
                                          it.get("download_url") or f"{self.s.tsdb_url}/get/{it.get('id')}",
                                          lang3(it.get("language")), it.get("release_name"),
                                          ai=bool(it.get("ai") or it.get("ai_transcribed")),
                                          hi=bool(it.get("hearing_impaired")), release_match=matched, hash_match=own,
                                          cues=it.get("cues") if isinstance(it.get("cues"), int) else None))
                offset += len(items)
                if not items or offset >= int(page.get("total") or 0):
                    return

        info_hash = str(hints.get("infoHash") or "").lower()
        if re.fullmatch(r"[0-9a-f]{40}", info_hash):
            # the torrent's own subtitles fit its video, but in a batch only those of the played episode (pages_of):
            # without a known episode they are ordinary candidates
            take(f"/v1/by-infohash/{info_hash}", query, hash_match=type_ != "series" or episode is not None)
        if name:
            take("/v1/by-releasename", dict(query, release=name), release=True)
        if imdb:
            _, id_season, id_episode = split_id(video_id)
            take(f"/v1/by-imdb/{imdb}" + (f"/season/{id_season}/episode/{id_episode}" if id_season is not None else ""),
                 query, pages=srclimits.TSDB_PAGES)
        if errors and len(errors) == tried:
            raise errors[0]
        return out

    # -- Gestdown (series) -------------------------------------------------------------------------------------
    def _gestdown(self, type_, video_id, langs, hints):
        """Cinemeta's TVDB id -> Gestdown's show -> one lookup per language. A show or a language Gestdown doesn't
        have (404) is no subtitles; one language's error doesn't drop the others; "being refreshed" (423)
        means ask again next time; "too many requests" (429) pauses Gestdown."""
        imdb, season, episode = split_id(video_id)
        if not imdb or season is None:
            return []
        def tvdb_of():
            try:
                return ((self._get(f"{self.s.cinemeta_url}/meta/series/{imdb}.json", lookup=True) or {}).get("meta")
                        or {}).get("tvdb_id")
            except NotFound:
                return None
            except (Limited, Busy):   # Cinemeta asked to be asked later: Gestdown gives nothing now, and isn't paused
                raise Busy() from None

        tvdb = self._id("tvdb", imdb, tvdb_of)
        if not tvdb:
            return []

        def show_of():
            try:
                shows = (self._get(f"{self.s.gestdown_url}/shows/external/tvdb/{tvdb}") or {}).get("shows") or []
            except NotFound:
                shows = []
            return shows[0]["id"] if shows else None

        show = self._id("gestdown", tvdb, show_of)
        if not show:
            return []
        out, errors, busy = [], [], False
        for lang in langs:
            try:
                got = self._get(f"{self.s.gestdown_url}/subtitles/get/{show}/{season}/{episode}/"
                                f"{urllib.parse.quote(lang_name(lang))}") or {}
            except NotFound:
                continue
            except Busy:
                busy = True
                continue
            except Limited:
                raise
            except Exception as e:
                errors.append(e)
                log.info("source gestdown %s/%s %s: %s", type_, video_id, lang,
                         f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__)
                continue
            for it in got.get("matchingSubtitles") or []:
                if not it.get("completed"):
                    continue
                out.append(self._cand("gestdown", f"gd-{it.get('subtitleId')}", f"{self.s.gestdown_url}{it.get('downloadUri')}",
                                      lang, it.get("version"), hi=bool(it.get("hearingImpaired"))))
        if busy and not out:
            raise Busy()
        if errors and len(errors) == len(langs):
            raise errors[0]
        return out

    # -- SubDL (key) -------------------------------------------------------------------------------------------
    def _subdl(self, type_, video_id, langs, hints):
        imdb, season, episode = split_id(video_id)
        codes = two_letter(langs)
        if not imdb or not codes:
            return []
        q = {"api_key": self.s.subdl_key, "imdb_id": imdb, "type": "tv" if season is not None else "movie",
             "languages": ",".join(code.upper() for code in codes), "subs_per_page": srclimits.SUBDL_PAGE_SIZE}
        if season is not None:
            q.update(season_number=season, episode_number=episode)
        self._count("subdl")
        got = self._get(f"{self.s.subdl_url}/api/v1/subtitles?{urllib.parse.urlencode(q)}") or {}
        out = []
        for it in got.get("subtitles") or []:
            url = it.get("url") or ""
            if not url:
                continue
            frag = f"#S{season:02d}E{episode:02d}" if season is not None else ""   # which file of a zip to take
            out.append(self._cand("subdl", f"subdl-{url.rsplit('/', 1)[-1]}", f"{self.s.subdl_download_url}{url}{frag}",
                                  lang3(it.get("language") or it.get("lang")), it.get("release_name"), hi=bool(it.get("hi"))))
        return out

    # -- SubSource (key) --------------------------------------------------------------------------------
    def _subsource(self, type_, video_id, langs, hints):
        """IMDb id (and season) -> SubSource's own id -> its subtitles in our languages; for an episode, those whose
        release names name another episode are left out. Downloads are zips, fetched with the key in a header."""
        imdb, season, episode = split_id(video_id)
        names = [SUBSOURCE_LANGS[x] for x in langs if x in SUBSOURCE_LANGS]
        if not imdb or not names:
            return []
        headers = {"X-API-Key": self.s.subsource_key}

        def movie_of():
            q = {"searchType": "imdb", "imdb": imdb}
            if season is not None:
                q["season"] = season
            self._count("subsource")
            try:
                got = self._get(f"{self.s.subsource_url}/movies/search?{urllib.parse.urlencode(q)}", headers)
            except NotFound:
                got = []
            movies = got if isinstance(got, list) else (got or {}).get("data") or []
            return (movies[0].get("id") or movies[0].get("movieId")) if movies else None

        movie = self._id("subsource", (imdb, season), movie_of)
        if not movie:
            return []
        self._count("subsource")
        got = self._get(f"{self.s.subsource_url}/subtitles?" + urllib.parse.urlencode(
            {"movieId": movie, "language": ",".join(names), "sort": "popular", "limit": srclimits.SUBSOURCE_PAGE_SIZE}),
            headers)
        items = got if isinstance(got, list) else (got or {}).get("data") or (got or {}).get("subtitles") or []
        frag = f"#S{season:02d}E{episode:02d}" if season is not None else ""
        out = []
        for it in items:
            sid = it.get("subtitleId") or it.get("id")
            if not sid:
                continue
            info = it.get("releaseInfo")
            release = " / ".join(info) if isinstance(info, list) else str(info or it.get("release_name") or "")
            if other_episode(release, season, episode):
                continue
            lang = SUBSOURCE_BY_NAME.get(str(it.get("language") or "").lower(), lang3(it.get("language")))
            out.append(self._cand("subsource", f"subsource-{sid}",
                                  f"{self.s.subsource_url}/subtitles/{sid}/download{frag}", lang, release,
                                  ai=bool(MACHINE.search(str(it.get("productionType") or ""))),
                                  hi=bool(it.get("hearingImpaired") or it.get("hi"))))
        return out

    # -- Wyzie (key, self-hosted only) ---------------------------------------------------------------------------
    def _wyzie(self, type_, video_id, langs, hints):
        imdb, season, episode = split_id(video_id)
        if not imdb:
            return []
        out = []
        for lang in langs:
            code = lang2(lang)
            if not code:
                continue
            q = {"id": imdb, "language": code, "key": self.s.wyzie_key}
            if season is not None:
                q.update(season=season, episode=episode)
            self._count("wyzie")
            got = self._get(f"{self.s.wyzie_url}/search?{urllib.parse.urlencode(q)}") or []
            for it in got if isinstance(got, list) else []:
                if not it.get("url"):
                    continue
                out.append(self._cand("wyzie", f"wyzie-{it.get('id')}", it["url"], lang3(it.get("language") or lang),
                                      it.get("release") or it.get("display"), hi=bool(it.get("isHearingImpaired"))))
        return out


def make_sources(settings):
    """The service's ExtraSources (one per process; in public mode shared by everyone, so answers and pauses are
    shared too), with its ledger in SUBSYNC_DATA/sources.sqlite3; None when no source is on. Each name
    SUBSYNC_EXTRA_SOURCES holds that it doesn't take (a keyed site, a misspelling) is said here, once, at the start
."""
    for name in settings.extra_sources_ignored:
        log.warning('SUBSYNC_EXTRA_SOURCES: "%s" ignored: it takes only osv3, osv3plus, tsdb and gestdown; SubDL, '
                    'SubSource and Wyzie Subs have their own key settings.', name)
    if not (settings.extra_sources or settings.subdl_key or settings.subsource_key or settings.wyzie_key):
        return None
    keyed = {"tsdb": bool(settings.tsdb_key), "subdl": bool(settings.subdl_key),
             "subsource": bool(settings.subsource_key), "wyzie": bool(settings.wyzie_key)}
    return ExtraSources(settings, ledger=Ledger(os.path.join(settings.data, "sources.sqlite3"), keyed=keyed))
