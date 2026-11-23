"""A title's original language, looked up by the id in the subtitle request, so every subtitle can be timed to the
file's track in that language (pipeline.choose_reference) and the audio route can fit the sound in it first
(audiofit.track_order).

Films and series by their IMDb id (an episode by its series' id): TMDB's original_language when a TMDB key is given,
and Wikidata's "original language of film or TV show" (P364) always, as the keyless fallback and a second opinion.
Anime known only by a MyAnimeList or Kitsu number: Japanese, unless AniList names another country of origin (a Kitsu
number is first turned into its MyAnimeList number on Kitsu). Only the id is sent, from this server; nothing about a
person, a file or a link.

Answers are kept per title in the store and shared by everyone: a found answer with no expiry, so a title is looked up
once, except one that holds TMDB's value, kept TMDB_KEEP_S (TMDB's terms allow 6 months) and then looked up again in
the background like one not found; not found 7 days and a failed service 10 minutes, so those are asked again. An
answer found without TMDB is asked at TMDB once a key comes, and only TMDB's answer replaces it (a TMDB that didn't
answer is asked again 10 minutes later). A lookup runs in the background and never holds a job: a file read before the
answer comes uses the rules for an unknown language, and is judged again once it is known (worker). An operator's list
(SUBSYNC_ORIGINAL_LANGUAGES) overrides any lookup.
"""
import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import digits, langs, net, realdebrid
from .sources import read_answer
from .version import user_agent

log = logging.getLogger("subsync.origlang")

USER_AGENT = user_agent("subsync")
TIMEOUT_S = 5.0
MAX_BYTES = 64 * 1024
DAY = 86400
NOT_FOUND_S = 7 * DAY        # a found answer is kept with no expiry, unless it holds TMDB's value:
TMDB_KEEP_S = 180 * DAY      # TMDB's terms allow keeping its data 6 months
FAILED_S = 600
PAUSE_S = 60                 # a 429 or 503 without Retry-After
PAUSE_MIN_S, PAUSE_MAX_S = 1.0, 3600.0   # the pause a Retry-After asks for is kept within these
BAD_KEY_S = 3600             # a key TMDB refused isn't tried again for an hour
MAX_PENDING = 200            # lookups waiting at once; a title past that is looked up on a later request
ANILIST_GAP_S = 2.0          # AniList allows 30 requests a minute at present
THREADS = 2
COUNTRY = {"JP": "jpn", "CN": "chi", "TW": "chi", "HK": "chi", "KR": "kor"}
WIKIDATA_QUERY = ('SELECT ?lang ?iso1 ?iso2 ?iso3 ?ietf WHERE {{ ?item wdt:P345 "{tt}" ; wdt:P364 ?lang . '
                  'OPTIONAL {{ ?lang wdt:P218 ?iso1 }} OPTIONAL {{ ?lang wdt:P219 ?iso2 }} '
                  'OPTIONAL {{ ?lang wdt:P220 ?iso3 }} OPTIONAL {{ ?lang wdt:P305 ?ietf }} }}')
ANILIST_QUERY = "query($id:Int){Media(idMal:$id,type:ANIME){countryOfOrigin}}"
V3_KEY = re.compile(r"[0-9a-fA-F]{32}")
# ASCII digits only: \d also takes other scripts' digits, which int would read as a different title
IMDB = re.compile(r"tt[0-9]{1,10}")
NUMBER = re.compile(r"[0-9]{1,9}")


class Failed(Exception):
    """A service didn't answer usably: a short reason (`why`) and the HTTP status (`code`, None without one); never an
    address or a key."""

    def __init__(self, service, why, code=None):
        super().__init__(f"{service}: {why}")
        self.why, self.code = why, code


_KEYED_OPENER = net.build_opener(net.NoRedirects())


def _key_in(key):
    """Where a TMDB key goes: (query parameters, headers). A v3 key (32 hex characters) in the address, a Read Access
    Token in the authorization header."""
    if V3_KEY.fullmatch(key or ""):
        return {"api_key": key}, {}
    return {}, {"authorization": f"Bearer {key}"}


def _open_keyed(req, timeout):
    """A request with a key in it (TMDB's): through the same checks as every request (Real-Debrid, the address rules),
    never following a redirect (urllib would send the key on to wherever it points)."""
    return realdebrid.urlopen(req, timeout, opener=_KEYED_OPENER)


def title_of(video_id):
    """The title a request's id is looked up as: an IMDb id (an episode's series), "mal:N" or "kitsu:N"; None for any
    other id."""
    parts = str(video_id or "").split(":")
    if IMDB.fullmatch(parts[0]):
        return parts[0]
    if len(parts) >= 2 and parts[0] in ("mal", "kitsu") and NUMBER.fullmatch(parts[1]):
        return f"{parts[0]}:{parts[1]}"
    return None


def parse_overrides(text):
    """SUBSYNC_ORIGINAL_LANGUAGES: "tt1375666=en,mal:5114=ja" -> ({"tt1375666": "eng", "mal:5114": "jpn"}, ignored).
    A key is a title as title_of names it: an IMDb id, or mal: or kitsu: and a number (an episode's id is
    none); `ignored` counts the entries that aren't such a key and a known language."""
    out, ignored = {}, 0
    for part in str(text or "").split(","):
        if not part.strip():
            continue
        title, _, code = part.partition("=")
        title, lang = title.strip(), langs.known(code.strip())
        if lang and title_of(title) == title:
            out[title] = lang
        else:
            ignored += 1
    return out, ignored


def _plain(track):
    from .pipeline import is_secondary
    return not track.get("forced") and not is_secondary(track)


def _found(answer):
    """Whether an answer holds a found language (TMDB's "none", a language outside ISO 639-1, included)."""
    return bool(answer and (answer.get("tmdb") or answer.get("wikidata") or answer.get("anime")))


def _from_tmdb(answer):
    """Whether an answer holds TMDB's value (its "none" included), which is kept at most TMDB_KEEP_S."""
    return bool(answer.get("tmdb"))


def choose(answer, tracks=()):
    """(the original language as a three-letter code or None, how it was decided). `tracks`: the file's text tracks
    (pipeline.text_references), used only when Wikidata lists several languages and TMDB gave none: of those, the one
    with the file's longest plain track."""
    if not answer:
        return None, "unknown"
    if answer.get("override"):
        return answer["override"], "override"
    tmdb = answer.get("tmdb")
    if tmdb and tmdb != "none":
        return tmdb, "tmdb"
    wikidata = answer.get("wikidata") or []
    if len(wikidata) == 1:
        return wikidata[0], "wikidata"
    if len(wikidata) > 1:
        best = None
        for t in tracks or ():
            if not _plain(t):
                continue
            lang = next((w for w in wikidata if langs.same_language(t["language"], w)), None)
            if lang and (best is None or len(t["cues"]) > best[1]):
                best = (lang, len(t["cues"]))
        return (best[0], "wikidata, by the file's tracks") if best else (None, "mixed")
    anime = answer.get("anime")
    if anime and anime != "none":
        return anime, answer.get("via") or "rule"
    return None, "none"


def disagree(answer):
    """TMDB's value not among Wikidata's (when both have one): kept as TMDB's, and shown."""
    tmdb, wikidata = (answer or {}).get("tmdb"), (answer or {}).get("wikidata") or []
    return bool(tmdb and tmdb != "none" and wikidata and not any(langs.same_language(tmdb, w) for w in wikidata))


def describe(answer, tracks=()):
    """What files.detail records of a title's original language: the language used, where from, and any disagreement."""
    lang, how = choose(answer, tracks)
    out = {"lang": lang, "via": how}
    if answer and not answer.get("override"):
        if answer.get("tmdb"):
            out["tmdb"] = answer["tmdb"]
        if answer.get("wikidata"):
            out["wikidata"] = list(answer["wikidata"])
        if disagree(answer):
            out["disagree"] = True
    return out


def current(expected):
    """Whether a verdict recorded under a rule still counts under `expected` (origlang.rule's): the same rule, or one
    recorded with a known original language under the same pass rule. So a file judged with its language isn't judged
    again because another request names it by an id with no answer (tmdb:, tvdb:) or another answer (kitsu: and
    tt…), and a file judged before its title's answer came is judged again once it comes."""
    base = str(expected).split(":", 1)[0]

    def ok(recorded):
        return recorded == expected or (isinstance(recorded, str) and recorded.startswith(base + ":"))
    return ok


def rule(rule_id, answer):
    """The rule a verdict is recorded under: the service's rule, plus the title's original language as it was known
    when the file was judged (several of Wikidata's when the file's tracks chose among them). Unknown: the rule alone, as
    before, so a file judged before the answer came is judged again once it is known, and only once."""
    lang, how = choose(answer)
    if lang:
        return f"{rule_id}:{lang}"
    if how == "mixed":
        return f"{rule_id}:{'+'.join(answer['wikidata'])}"
    return rule_id


def check_key(settings, key, timeout=10.0):
    """Whether TMDB takes a key (one request, GET /configuration, through the request gate): True, False, or None when
    TMDB didn't answer. The key is never logged or put in an error."""
    params, auth = _key_in(key)
    url = f"{settings.tmdb_url}/configuration" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    try:
        with _open_keyed(urllib.request.Request(url, headers={"user-agent": USER_AGENT, **auth}), timeout=timeout) as r:
            r.read(MAX_BYTES)
            return True
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code in (401, 403):
            return False
        log.info("TMDB key check: HTTP %d", code)
        return None
    except Exception as e:
        log.info("TMDB key check: TMDB didn't answer (%s)", type(e).__name__)
        return None


def make_lookup(settings, store, clock=time.time):
    """The service's Lookup (one per process; in public mode shared by everyone). None with SUBSYNC_ORIGINAL_LANGUAGE
    off, which also deletes the answers kept. TMDB's answers kept with no expiry (by a version before TMDB_KEEP_S) get
    theirs, counted from when they were found."""
    if not settings.original_language:
        store.title_languages_clear()
        return None
    if settings.original_languages_ignored:
        log.warning("SUBSYNC_ORIGINAL_LANGUAGES: %d entries ignored (not an id=language pair)",
                    settings.original_languages_ignored)
    store.title_languages_expiring(TMDB_KEEP_S, _from_tmdb)
    return Lookup(settings, store, clock=clock)


class Lookup:
    def __init__(self, settings, store, clock=time.time):
        self.s, self.store, self.clock = settings, store, clock
        self.timeout = TIMEOUT_S
        self.anilist_gap = ANILIST_GAP_S
        self.overrides = dict(settings.original_languages or {})
        self.pool = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="subsync-language")
        self.lock = threading.Lock()
        self.flying = {}                  # title -> its running lookup (a Future)
        self.paused = {}                  # service -> clock time it may be asked again
        self.bad_keys = {}                # a refused key's SHA-256 -> clock time it may be tried again
        self.tmdb_later = {}              # title TMDB didn't answer for -> clock time it may be asked again
        self.max_pending = MAX_PENDING
        self.wikidata_lock = threading.Lock()
        self.anilist_lock = threading.Lock()
        self.anilist_last = 0.0

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    # -- what the worker calls ---------------------------------------------------------------------------------
    def peek(self, video_id):
        """The title's answer as kept, or None; never asks anyone."""
        title = title_of(video_id)
        if title is None:
            return None
        if title in self.overrides:
            return {"override": self.overrides[title], "via": "override"}
        return self.store.title_language(title, self.clock())

    def _usable(self, key):
        """The key, unless TMDB refused it within the hour (then none: Wikidata alone)."""
        if not key:
            return ""
        return "" if self.bad_keys.get(hashlib.sha256(key.encode()).hexdigest(), 0) > self.clock() else key

    def start(self, video_id, key=""):
        """Starts the title's lookup in the background unless its answer is kept (an answer found without TMDB is
        looked up again with a key, once TMDB answers: FAILED_S after a try it didn't), an override names it, or it is
        running already (then that lookup). Returns its Future, or None; never waits. Past MAX_PENDING lookups waiting,
        None: a later request asks again."""
        title = title_of(video_id)
        if title is None or title in self.overrides:
            return None
        key = self._usable(key)
        with self.lock:
            if title in self.flying:
                return self.flying[title]
            now = self.clock()
            kept = self.store.title_language(title, now)
            if kept is not None and not (key and title.startswith("tt") and not kept.get("tmdb_asked")
                                         and self.tmdb_later.get(title, 0) <= now):
                return None
            if len(self.flying) >= self.max_pending:
                return None
            fut = self.pool.submit(self._lookup, title, key, kept if _found(kept) else None)
            self.flying[title] = fut
        fut.add_done_callback(lambda _f, t=title: self._landed(t))
        return fut

    def _landed(self, title):
        with self.lock:
            self.flying.pop(title, None)

    # -- one lookup --------------------------------------------------------------------------------------------
    def _lookup(self, title, key, kept=None):
        """`kept`: the title's found answer, when it is looked up again for TMDB's (start). Only TMDB's answer replaces
        it: while TMDB doesn't answer, it stays as it was, with no expiry, and when Wikidata doesn't, its Wikidata part
        stays. A found answer that holds TMDB's value is kept TMDB_KEEP_S."""
        answer = {"tmdb": None, "tmdb_asked": False, "wikidata": None, "anime": None, "via": None, "failed": []}
        try:
            if title.startswith(("mal:", "kitsu:")):
                self._anime(title, answer)
            else:
                if key:
                    try:
                        answer["tmdb"] = self._tmdb(title, key)
                        answer["tmdb_asked"] = True
                    except Failed as e:
                        answer["failed"].append("tmdb")
                        log.info("original language %s: TMDB didn't answer (%s)", title, e.why)
                        if e.code in (401, 403):     # the key itself: not tried again for an hour
                            now = self.clock()
                            self._set_aside(self.bad_keys, hashlib.sha256(key.encode()).hexdigest(), now + BAD_KEY_S,
                                            now)
                try:
                    answer["wikidata"] = self._wikidata(title)
                except Failed as e:
                    answer["failed"].append("wikidata")
                    log.info("original language %s: Wikidata didn't answer (%s)", title, e.why)
                lang, how = choose(answer)
                answer["via"] = how if lang else None
        except Exception as e:      # never let a lookup take anything down with it
            answer["failed"].append("lookup")
            log.warning("original language %s: lookup failed (%s)", title, type(e).__name__)
        now = self.clock()
        if key and title.startswith("tt"):   # TMDB was asked: when it didn't answer, start asks it again FAILED_S later
            if answer["tmdb_asked"]:
                with self.lock:
                    self.tmdb_later.pop(title, None)
            else:
                self._set_aside(self.tmdb_later, title, now + FAILED_S, now)
        if kept is not None:
            if not answer["tmdb_asked"]:
                log.info("original language %s: kept as found before (TMDB asked again later)", title)
                return kept
            if answer["wikidata"] is None:   # Wikidata didn't answer this time
                answer["wikidata"] = kept.get("wikidata")
                lang, how = choose(answer)
                answer["via"] = how if lang else None
        found = _found(answer)
        if answer["failed"] and kept is None:
            until = now + FAILED_S
        elif not found:
            until = now + NOT_FOUND_S
        else:
            until = now + TMDB_KEEP_S if _from_tmdb(answer) else None    # found: TMDB's 6 months, else no expiry
        answer["wikidata"] = answer["wikidata"] or []
        self.store.title_language_put(title, answer, now, until)
        lang, how = choose(answer)
        others = f" (Wikidata: {', '.join(answer['wikidata'])})" if disagree(answer) or len(answer["wikidata"]) > 1 \
            else ""
        if lang:
            source = {"tmdb": "TMDB", "wikidata": "Wikidata", "anilist": "AniList", "rule": "the anime rule"}.get(how, how)
            log.info("original language %s: %s from %s%s", title, lang, source, others)
        elif how == "mixed":
            log.info("original language %s: several%s, chosen by each file's tracks", title, others)
        elif answer["failed"]:
            log.info("original language %s: unknown for now (%s didn't answer)", title, ", ".join(answer["failed"]))
        else:
            log.info("original language %s: not found", title)
        return answer

    def _set_aside(self, times, key, until, now):
        """times[key] = until, the clock time `key` may be tried again. The entries whose time has passed go first
: they mean nothing any more, and the keys (titles, refused keys) come from requests."""
        with self.lock:
            for passed in [k for k, at in times.items() if at <= now]:
                del times[passed]
            times[key] = until

    def _paused(self, service):
        return self.paused.get(service, 0) > self.clock()

    def _get(self, service, url, headers=None, data=None):
        """JSON from one service through the request gate (TMDB's, which carry a key, never following a redirect).
        Any failure becomes Failed(service, reason): the address (which may hold a key) is never in it, and no exception
        is chained."""
        if self._paused(service):
            raise Failed(service, "paused after too many requests")
        req = urllib.request.Request(url, data=data, headers={"user-agent": USER_AGENT, **(headers or {})})
        why, code = None, None
        try:
            body = read_answer(req, self.timeout, MAX_BYTES + 1, _open_keyed if service == "tmdb" else None)
            if len(body) > MAX_BYTES:
                why = "answer too large"
            else:
                return json.loads(body or b"null")
        except urllib.error.HTTPError as e:
            code = e.code
            retry = e.headers.get("Retry-After") if e.headers else None
            e.close()
            if code == 404:
                return None
            why = f"HTTP {code}"
            if code in (429, 503):
                wait = net.retry_after(retry)
                wait = PAUSE_S if wait is None else min(max(wait, PAUSE_MIN_S), PAUSE_MAX_S)
                self.paused[service] = self.clock() + wait
        except (urllib.error.URLError, OSError, ValueError) as e:
            why = "timed out" if "timed out" in str(getattr(e, "reason", e)) else type(e).__name__
        except Exception as e:
            why = type(e).__name__
        raise Failed(service, why, code)

    def _tmdb(self, tt, key):
        """TMDB's original_language for an IMDb id: "eng", "none" (no language, or a code outside ISO 639-1) or None
        (not on TMDB). The first film found, else the first series; its details are asked when the find answer has no
        original_language."""
        params, auth = _key_in(key)
        base = self.s.tmdb_url
        query = urllib.parse.urlencode({"external_source": "imdb_id", **params})
        got = self._get("tmdb", f"{base}/find/{tt}?{query}", auth) or {}
        movies, shows = got.get("movie_results") or [], got.get("tv_results") or []
        kind, items = ("movie", movies) if movies else ("tv", shows)
        if not items:
            return None
        item = items[0]
        code = item.get("original_language")
        tmdb_id = digits.whole(item.get("id", ""))
        if code is None and tmdb_id is not None:
            query = f"?{urllib.parse.urlencode(params)}" if params else ""
            code = (self._get("tmdb", f"{base}/{kind}/{tmdb_id}{query}", auth) or {}).get("original_language")
        if code is None:
            return None
        return langs.from_tmdb(code) or "none"

    def _wikidata(self, tt):
        """Wikidata's original languages of an IMDb id, in the order the query service gave them, each once."""
        url = self.s.wikidata_url + "?" + urllib.parse.urlencode({"query": WIKIDATA_QUERY.format(tt=tt),
                                                                  "format": "json"})
        with self.wikidata_lock:
            got = self._get("wikidata", url, {"accept": "application/sparql-results+json"}) or {}
        items = {}
        for b in (got.get("results") or {}).get("bindings") or []:
            item = (b.get("lang") or {}).get("value")
            if not item:
                continue
            codes = items.setdefault(item, {})
            for k in ("iso1", "iso2", "iso3", "ietf"):
                v = (b.get(k) or {}).get("value")
                if v:
                    codes.setdefault(k, v)
        out = []
        for codes in items.values():
            lang = next((langs.known(codes[k]) for k in ("iso1", "iso2", "iso3", "ietf")
                         if k in codes and langs.known(codes[k])), None)
            if lang and lang not in out:
                out.append(lang)
        return out

    def _anime(self, title, answer):
        kind, number = title.split(":")
        mal = number
        if kind == "kitsu":
            try:
                mal = self._kitsu_mal(number)
            except Failed:
                answer["failed"].append("kitsu")
                mal = None
            if mal is None:
                answer["anime"], answer["via"] = "jpn", "rule"
                return
        try:
            country = self._anilist(int(mal))
        except Failed:
            answer["failed"].append("anilist")
            country = None
        if country is None:
            answer["anime"], answer["via"] = "jpn", "rule"
        else:
            answer["anime"], answer["via"] = COUNTRY.get(country, "none"), "anilist"

    def _kitsu_mal(self, number):
        query = urllib.parse.urlencode({"include": "mappings", "fields[anime]": "id",
                                        "fields[mappings]": "externalSite,externalId"})
        got = self._get("kitsu", f"{self.s.kitsu_url}/anime/{number}?{query}",
                        {"accept": "application/vnd.api+json"}) or {}
        for inc in got.get("included") or []:
            attrs = inc.get("attributes") or {}
            if inc.get("type") == "mappings" and attrs.get("externalSite") == "myanimelist/anime" \
                    and NUMBER.fullmatch(str(attrs.get("externalId") or "")):
                return str(attrs["externalId"])
        return None

    def _anilist(self, mal):
        """AniList's country of origin for a MyAnimeList number ("JP"), or None when AniList doesn't know it."""
        body = json.dumps({"query": ANILIST_QUERY, "variables": {"id": mal}}).encode()
        with self.anilist_lock:
            wait = self.anilist_last + self.anilist_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                got = self._get("anilist", self.s.anilist_url, {"content-type": "application/json",
                                                               "accept": "application/json"}, data=body)
            finally:
                self.anilist_last = time.monotonic()
        media = ((got or {}).get("data") or {}).get("Media") or {}
        country = media.get("countryOfOrigin")
        return str(country).upper() if country else None
