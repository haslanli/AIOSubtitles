"""A title's original language, looked up by its id: TMDB (with a key) and Wikidata for IMDb ids, Kitsu and AniList for
anime ids; kept per title in the store, shared, never holding a job. A local stand-in answers like the four services;
no network."""
import json
import logging
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import Clock, closed_after, LookupServices, start_server, stop_server, TempDir  # noqa: E402
from subsync import origlang  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.store import Store  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"            # a made-up v3 key (32 hex characters)
TOKEN = "eyJhbGciOiJIUzI1NiJ9.made-up-token-for-tests.c2lnbmF0dXJl"   # a made-up Read Access Token
DAY = 86400
Q = "http://www.wikidata.org/entity/"


def binding(item, iso1=None, iso2=None, iso3=None, ietf=None):
    b = {"lang": {"type": "uri", "value": Q + item}}
    for k, v in (("iso1", iso1), ("iso2", iso2), ("iso3", iso3), ("ietf", ietf)):
        if v:
            b[k] = {"type": "literal", "value": v}
    return b


class Base(unittest.TestCase):
    key = KEY

    def setUp(self):
        td = TempDir()
        self.tmp = td.__enter__()
        self.addCleanup(td.__exit__, None, None, None)
        self.srv = LookupServices()
        self.addCleanup(self.srv.close)
        self.clock = Clock()
        self.store = closed_after(self, Store(self.tmp, clock=self.clock))
        self.env = {"SUBSYNC_DATA": self.tmp, "SUBSYNC_TMDB_URL": self.srv.base + "/tmdb",
                    "SUBSYNC_WIKIDATA_URL": self.srv.base + "/sparql", "SUBSYNC_KITSU_URL": self.srv.base + "/kitsu",
                    "SUBSYNC_ANILIST_URL": self.srv.base + "/anilist", "SUBSYNC_TMDB_KEY": self.key or ""}
        self.lookup = self.make()

    def make(self, **env):
        s = Settings(dict(self.env, **env))
        lk = origlang.make_lookup(s, self.store, clock=self.clock)
        if lk is not None:
            lk.timeout = 1.0
            lk.anilist_gap = 0.0
            self.addCleanup(lk.close)
        return lk

    def ask(self, video_id, key=None, lookup=None):
        """Starts the lookup and waits for it (only tests wait; a job never does)."""
        lk = lookup or self.lookup
        fut = lk.start(video_id, key=self.key if key is None else key)
        if fut is not None:
            fut.result(timeout=10)
        return lk.peek(video_id)


class IdsTest(unittest.TestCase):
    def test_the_title_looked_up(self):
        for video_id, title in (("tt1375666", "tt1375666"), ("tt0944947:1:1", "tt0944947"),
                                ("tt0944947:12:104", "tt0944947"), ("mal:5114", "mal:5114"),
                                ("mal:5114:3", "mal:5114"), ("kitsu:1376", "kitsu:1376"),
                                ("kitsu:1376:12", "kitsu:1376")):
            self.assertEqual(origlang.title_of(video_id), title, video_id)

    def test_other_ids_are_not_looked_up(self):
        for video_id in ("tmdb:603", "tvdb:121361", "tt", "tt12ab", "tt12345678901", "mal:x", "kitsu:", "mal:1234567890",
                         "", None, "tt1375666 OR 1", 'tt1" . ?x ?y ?z'):
            self.assertIsNone(origlang.title_of(video_id), video_id)

    def test_an_id_in_other_digits_is_not_looked_up(self):
        # \d also took other scripts' digits: "mal:١٢" was looked up as MyAnimeList's 12 (int reads them)
        for video_id in ("mal:\u0661\u0662", "kitsu:\uff11", "tt\u0661\u0662", "tt\uff11:1:1", "mal:\u00b2"):
            self.assertIsNone(origlang.title_of(video_id), video_id)


class TmdbTest(Base):
    def test_a_film(self):
        self.srv.find["tt1375666"] = {"movie_results": [{"id": 27205, "original_language": "en"}], "tv_results": []}
        got = self.ask("tt1375666")
        self.assertEqual(got["tmdb"], "eng")
        self.assertEqual(origlang.choose(got)[0], "eng")
        (call,) = self.srv.of("tmdb")
        self.assertEqual(call["path"], "/tmdb/find/tt1375666")
        q = urllib.parse.parse_qs(call["query"])
        self.assertEqual(q["external_source"], ["imdb_id"])
        self.assertEqual(q["api_key"], [KEY])                 # a v3 key goes only as the api_key parameter
        self.assertNotIn("authorization", call["headers"])

    def test_a_read_access_token_goes_in_the_header(self):
        self.srv.find["tt1375666"] = {"movie_results": [{"id": 27205, "original_language": "en"}]}
        self.ask("tt1375666", key=TOKEN)
        (call,) = self.srv.of("tmdb")
        self.assertEqual(call["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertNotIn("api_key", call["query"])

    def test_an_episode_is_looked_up_by_its_series(self):
        self.srv.find["tt0944947"] = {"movie_results": [], "tv_results": [{"id": 1399, "original_language": "en"}]}
        got = self.ask("tt0944947:1:1", key=KEY)
        self.assertEqual(got["tmdb"], "eng")
        self.assertEqual([c["path"] for c in self.srv.of("tmdb")], ["/tmdb/find/tt0944947"])
        self.assertEqual(self.lookup.peek("tt0944947:3:5"), got)      # the whole series, once

    def test_a_series_item_without_the_field_asks_its_details(self):
        self.srv.find["tt2788316"] = {"tv_results": [{"id": 126308, "name": "Shogun"}]}
        self.srv.details[("tv", "126308")] = {"id": 126308, "original_language": "en"}
        got = self.ask("tt2788316:1:1")
        self.assertEqual(got["tmdb"], "eng")
        self.assertEqual([c["path"] for c in self.srv.of("tmdb")], ["/tmdb/find/tt2788316", "/tmdb/tv/126308"])

    def test_an_item_whose_id_is_no_ascii_number_asks_no_details(self):
        # Isdigit took "²" and other scripts' digits for TMDB's id; asking its details by it failed before
        # anything was sent (no ASCII address), so TMDB was taken for not answering, as if it were down
        for tt, item_id in (("tt2788317", "\u00b2"), ("tt2788318", "\u0661\u0662")):
            with self.subTest(item_id=item_id):
                self.srv.find[tt] = {"tv_results": [{"id": item_id, "name": "Shogun"}]}
                got = self.ask(tt + ":1:1")
                self.assertEqual((got["tmdb"], got["tmdb_asked"], got["failed"]), (None, True, []))
                self.assertEqual([c["path"] for c in self.srv.of("tmdb") if not c["path"].startswith("/tmdb/find/")],
                                 [])

    def test_tmdbs_own_codes(self):
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "original_language": "cn"}]}
        self.srv.find["tt2"] = {"movie_results": [{"id": 2, "original_language": "xx"}]}
        self.srv.wikidata["tt2"] = [binding("Q1860", iso1="en")]
        self.assertEqual(origlang.choose(self.ask("tt1"))[0], "chi")
        got = self.ask("tt2")
        self.assertEqual(got["tmdb"], "none")                        # "no language": Wikidata's answer is used
        self.assertEqual(origlang.choose(got), ("eng", "wikidata"))

    def test_without_a_key_tmdb_isnt_asked(self):
        self.srv.wikidata["tt0047478"] = [binding("Q5287", iso1="ja")]
        got = self.ask("tt0047478", key="")
        self.assertEqual(self.srv.of("tmdb"), [])
        self.assertIsNone(got["tmdb"])
        self.assertEqual(origlang.choose(got), ("jpn", "wikidata"))


class FailureTest(Base):
    def assert_no_secret(self, logs, got):
        text = "\n".join(logs) + json.dumps(got) + json.dumps(self.store.db.execute(
            "SELECT * FROM title_language").fetchall())
        self.assertNotIn(KEY, text)
        self.assertNotIn("api_key", text)
        self.assertNotIn(self.srv.base, text)

    def failing(self, code=None, headers=(), delay=None, url=None):
        if code:
            self.srv.status["tmdb"] = (code, headers)
        if delay:
            self.srv.delay["tmdb"] = delay
        lk = self.make(SUBSYNC_TMDB_URL=url) if url else self.lookup
        self.srv.wikidata["tt1375666"] = [binding("Q1860", iso1="en")]
        with self.assertLogs("subsync", level="DEBUG") as cm:
            logging.getLogger("subsync").debug("start")
            try:
                got = self.ask("tt1375666", lookup=lk)
            except Exception as e:      # nothing may escape, least of all with the key in it
                self.fail(f"{type(e).__name__}: {e}")
        return got, cm.output

    def test_failures_keep_the_key_out_and_retry_after_ten_minutes(self):
        for name, kw in (("refused", {"code": 401}), ("server error", {"code": 500}),
                         ("too slow", {"delay": 2.0}), ("unreachable", {"url": "http://127.0.0.1:9/3"})):
            with self.subTest(name):
                self.store.db.execute("DELETE FROM title_language")
                self.srv.status.pop("tmdb", None)
                self.srv.delay.pop("tmdb", None)
                self.lookup.bad_keys.clear()       # a refused key is set aside for an hour (KeySafetyTest)
                got, logs = self.failing(**kw)
                self.assertIn("tmdb", got["failed"])
                self.assertEqual(origlang.choose(got), ("eng", "wikidata"))   # what answered is used meanwhile
                until = self.store.db.execute("SELECT until FROM title_language").fetchone()[0]
                self.assertEqual(until, self.clock() + 600)
                self.assert_no_secret(logs, got)

    def test_too_many_requests_pauses_the_service(self):
        got, logs = self.failing(code=429, headers=(("Retry-After", "120"),))
        self.assertIn("tmdb", got["failed"])
        self.srv.status.pop("tmdb")
        self.srv.find["tt0816692"] = {"movie_results": [{"id": 157336, "original_language": "en"}]}
        n = len(self.srv.of("tmdb"))
        got = self.ask("tt0816692")
        self.assertEqual(len(self.srv.of("tmdb")), n)              # paused: not asked
        self.assertIn("tmdb", got["failed"])
        self.clock.t += 121
        self.clock.t += 601                                       # the failed row's ten minutes too
        got = self.ask("tt0816692")
        self.assertEqual(got["tmdb"], "eng")
        self.assert_no_secret(logs, got)

    def test_a_retry_after_that_is_no_number_pauses_for_the_default(self):
        # "nan" paused the service for a second, not PAUSE_S (every comparison with it is false)
        for asked, pause in (("nan", origlang.PAUSE_S), ("120", 120), ("0", origlang.PAUSE_MIN_S)):
            with self.subTest(asked=asked):
                self.store.db.execute("DELETE FROM title_language")
                self.lookup.paused.clear()
                self.failing(code=429, headers=(("Retry-After", asked),))
                self.assertEqual(self.lookup.paused["tmdb"], self.clock() + pause)

    def test_not_found(self):
        got = self.ask("tt9999999")
        self.assertEqual((got["tmdb"], got["wikidata"], got["failed"]), (None, [], []))
        self.assertEqual(origlang.choose(got), (None, "none"))
        until = self.store.db.execute("SELECT until FROM title_language").fetchone()[0]
        self.assertEqual(until, self.clock() + 7 * DAY)


class Elsewhere:
    """Another host a redirect could point to: records whatever reaches it."""

    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.calls.append((self.path, self.headers.get("authorization")))
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        self.srv = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        stop_server(self.srv)


class KeySafetyTest(Base):
    def test_a_redirect_is_never_followed_with_the_key(self):
        away = Elsewhere()
        self.addCleanup(away.close)
        self.srv.status["tmdb"] = (302, (("Location", away.url + "/3/find/tt1375666?external_source=imdb_id"),))
        self.srv.wikidata["tt1375666"] = [binding("Q1860", iso1="en")]
        for key in (TOKEN, KEY):
            self.store.db.execute("DELETE FROM title_language")
            got = self.ask("tt1375666", key=key)
            self.assertIn("tmdb", got["failed"])
            self.assertIs(origlang.check_key(Settings(dict(self.env)), key), None)
        self.assertEqual(away.calls, [])

    def test_a_refused_key_is_set_aside_for_an_hour(self):
        self.srv.status["tmdb"] = (401, ())
        self.srv.wikidata["tt1"] = [binding("Q1860", iso1="en")]
        self.srv.wikidata["tt2"] = [binding("Q1860", iso1="en")]
        got = self.ask("tt1")
        self.assertIn("tmdb", got["failed"])
        n = len(self.srv.of("tmdb"))
        got = self.ask("tt2")
        self.assertEqual(len(self.srv.of("tmdb")), n)                  # the same key isn't tried again
        self.assertEqual((got["failed"], got["tmdb_asked"]), ([], False))
        self.assertEqual(origlang.choose(got), ("eng", "wikidata"))

    def test_a_wrong_address_is_not_a_refused_key(self):
        self.srv.status["tmdb"] = (404, ())
        self.assertIsNone(origlang.check_key(Settings(dict(self.env)), KEY))

    def test_the_queue_is_bounded(self):
        self.lookup.max_pending = 2
        self.srv.delay["tmdb"] = 0.5
        futs = [self.lookup.start(f"tt{n}", key=KEY) for n in (11, 12, 13)]
        self.assertIsNone(futs[2])                                     # left for a later request
        for f in futs[:2]:
            f.result(timeout=10)
        self.assertNotIn("/tmdb/find/tt13", [c["path"] for c in self.srv.of("tmdb")])

    def test_close_cancels_what_is_queued(self):
        self.srv.delay["sparql"] = 0.5
        futs = [self.lookup.start(f"tt{n}", key="") for n in range(21, 27)]
        self.lookup.close()
        self.assertTrue(any(f.cancelled() for f in futs))


class TmdbLaterTest(Base):
    def test_a_title_found_without_a_key_is_asked_at_tmdb_once_a_key_comes(self):
        self.srv.wikidata["tt1375666"] = [binding("Q5287", iso1="ja"), binding("Q1860", iso1="en"),
                                          binding("Q150", iso1="fr")]
        self.srv.find["tt1375666"] = {"movie_results": [{"id": 27205, "original_language": "en"}]}
        got = self.ask("tt1375666", key="")
        self.assertFalse(got["tmdb_asked"])
        self.assertEqual(origlang.choose(got), (None, "mixed"))
        fut = self.lookup.start("tt1375666", key=KEY)                   # a key now: TMDB is asked after all
        self.assertIsNotNone(fut)
        fut.result(timeout=10)
        got = self.lookup.peek("tt1375666")
        self.assertTrue(got["tmdb_asked"])
        self.assertEqual(origlang.choose(got), ("eng", "tmdb"))
        self.assertIsNone(self.lookup.start("tt1375666", key=KEY))       # and then never again
        self.assertIsNone(self.lookup.start("tt1375666", key=""))
        self.assertEqual(len(self.srv.of("tmdb")), 1)

    def until(self, title):
        return self.store.db.execute("SELECT until FROM title_language WHERE title=?", (title,)).fetchone()[0]

    def again(self, title):
        fut = self.lookup.start(title, key=KEY)
        self.assertIsNotNone(fut)
        fut.result(timeout=10)
        return self.lookup.peek(title)

    def test_a_found_answer_stays_while_tmdb_doesnt_answer(self):
        # Only TMDB's answer replaces a found one; while it doesn't come the answer stays, with no expiry, and
        # TMDB is asked again ten minutes later, not on every request
        self.srv.wikidata["tt1"] = [binding("Q1860", iso1="en")]
        self.ask("tt1", key="")
        self.srv.status["tmdb"] = (500, ())
        self.srv.status["sparql"] = (500, ())
        got = self.again("tt1")
        self.assertEqual((origlang.choose(got), got["tmdb_asked"]), (("eng", "wikidata"), False))
        self.assertIsNone(self.until("tt1"))
        self.assertIsNone(self.lookup.start("tt1", key=KEY))
        self.assertEqual(len(self.srv.of("tmdb")), 1)
        self.clock.t += 601
        self.srv.status.clear()
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "original_language": "fr"}]}
        got = self.again("tt1")
        self.assertEqual((origlang.choose(got), got["tmdb_asked"]), (("fre", "tmdb"), True))
        self.assertEqual(len(self.srv.of("tmdb")), 2)

    def test_a_found_answer_keeps_its_wikidata_part_when_only_wikidata_fails(self):
        self.srv.wikidata["tt1"] = [binding("Q150", iso1="fr"), binding("Q1860", iso1="en")]
        self.ask("tt1", key="")
        self.srv.status["sparql"] = (500, ())
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "original_language": "en"}]}
        got = self.again("tt1")
        self.assertEqual((got["wikidata"], origlang.choose(got)), (["fre", "eng"], ("eng", "tmdb")))
        self.assertEqual(self.until("tt1"), self.clock() + 180 * DAY)      # TMDB's value: its 6 months

    def test_a_tmdb_that_failed_is_not_asked_again_at_once(self):
        self.srv.status["tmdb"] = (500, ())
        self.srv.wikidata["tt1"] = [binding("Q1860", iso1="en")]
        self.ask("tt1")
        self.assertIsNone(self.lookup.start("tt1", key=KEY))           # the failed row's ten minutes hold
        self.assertEqual(len(self.srv.of("tmdb")), 1)


class WikidataTest(Base):
    key = ""

    def test_the_query(self):
        self.srv.wikidata["tt0047478"] = [binding("Q5287", iso1="ja", iso2="jpn")]
        self.assertEqual(self.ask("tt0047478")["wikidata"], ["jpn"])
        (call,) = self.srv.of("sparql")
        q = urllib.parse.parse_qs(call["query"])
        self.assertEqual(q["format"], ["json"])
        self.assertIn('wdt:P345 "tt0047478"', q["query"][0])
        self.assertIn("wdt:P364", q["query"][0])
        self.assertEqual(call["headers"]["accept"], "application/sparql-results+json")
        # Wikidata's terms ask for contact details: the project's address (and the release's whole version)
        self.assertRegex(call["headers"]["user-agent"],
                         r"^AIOSubtitles/\d+\.\d+\.\d+ \(subsync; \+https://github\.com/haslanli/AIOSubtitles\)$")

    def test_several_values_dialects_and_other_codes(self):
        self.srv.wikidata["tt1375666"] = [binding("Q5287", iso1="ja"), binding("Q1860", iso1="en"),
                                          binding("Q150", iso1="fr", iso2="fre"), binding("Q150", iso1="fr", iso2="fra")]
        self.srv.wikidata["tt0903747"] = [binding("Q7976", ietf="en-US")]           # "American English"
        self.srv.wikidata["tt0000001"] = [binding("Q9186", iso3="yue", ietf="yue")]  # Cantonese: no 639-1 code
        self.srv.wikidata["tt0000002"] = [binding("Q7850", iso2="chi")]
        self.srv.wikidata["tt0000003"] = [binding("Q22282914")]                       # no code at all
        self.assertEqual(self.ask("tt1375666")["wikidata"], ["jpn", "eng", "fre"])
        self.assertEqual(self.ask("tt0903747")["wikidata"], ["eng"])
        self.assertEqual(self.ask("tt0000001")["wikidata"], ["yue"])
        self.assertEqual(self.ask("tt0000002")["wikidata"], ["chi"])
        self.assertEqual(self.ask("tt0000003")["wikidata"], [])

    def test_one_query_at_a_time(self):
        self.srv.delay["sparql"] = 0.3
        futs = [self.lookup.start(f"tt100000{i}", key="") for i in range(4)]
        for f in futs:
            f.result(timeout=10)
        self.assertEqual(self.srv.most_active["sparql"], 1)

    def test_tmdb_answers_and_wikidata_is_asked_too(self):
        self.srv.find["tt2788316"] = {"tv_results": [{"id": 126308, "original_language": "en"}]}
        self.srv.wikidata["tt2788316"] = [binding("Q5287", iso1="ja"), binding("Q1860", iso1="en")]
        got = self.ask("tt2788316:1:1", key=KEY)
        self.assertEqual((got["tmdb"], got["wikidata"]), ("eng", ["jpn", "eng"]))


class ChooseTest(unittest.TestCase):
    def track(self, lang, lines, name="", forced=False):
        return {"language": lang, "cues": [(i * 3.0, 2.0) for i in range(lines)], "name": name, "forced": forced}

    def test_tmdb_first_and_a_disagreement_recorded(self):
        a = {"tmdb": "eng", "wikidata": ["jpn"], "anime": None, "failed": []}
        self.assertEqual(origlang.choose(a), ("eng", "tmdb"))
        self.assertTrue(origlang.disagree(a))
        self.assertFalse(origlang.disagree({"tmdb": "eng", "wikidata": ["jpn", "eng"]}))   # Shogun: one of them
        self.assertFalse(origlang.disagree({"tmdb": "chi", "wikidata": ["yue"]}))          # one language, by family
        self.assertFalse(origlang.disagree({"tmdb": "eng", "wikidata": []}))

    def test_several_wikidata_values_are_chosen_by_the_files_tracks(self):
        a = {"tmdb": None, "wikidata": ["jpn", "eng", "fre"]}
        self.assertEqual(origlang.choose(a, [self.track("eng", 900), self.track("fre", 300)]),
                         ("eng", "wikidata, by the file's tracks"))
        self.assertEqual(origlang.choose(a, [self.track("spa", 900)]), (None, "mixed"))
        self.assertEqual(origlang.choose(a), (None, "mixed"))
        # plain tracks only: an SDH or a forced track doesn't count
        self.assertEqual(origlang.choose(a, [self.track("eng", 900, "English SDH"), self.track("fre", 300),
                                             self.track("jpn", 50, forced=True)]),
                         ("fre", "wikidata, by the file's tracks"))

    def test_the_rest(self):
        self.assertEqual(origlang.choose({"override": "kor"}), ("kor", "override"))
        self.assertEqual(origlang.choose({"tmdb": None, "wikidata": ["rus"]}), ("rus", "wikidata"))
        self.assertEqual(origlang.choose({"anime": "jpn", "via": "anilist"}), ("jpn", "anilist"))
        self.assertEqual(origlang.choose({"anime": "jpn", "via": "rule"}), ("jpn", "rule"))
        self.assertEqual(origlang.choose({"anime": "none", "via": "anilist"}), (None, "none"))
        self.assertEqual(origlang.choose({"tmdb": "none", "wikidata": []}), (None, "none"))
        self.assertEqual(origlang.choose(None), (None, "unknown"))


class AnimeTest(Base):
    def test_a_mal_id_asks_anilist(self):
        for mal, country, lang in ((5114, "JP", "jpn"), (1, "CN", "chi"), (2, "TW", "chi"), (3, "HK", "chi"),
                                   (4, "KR", "kor"), (5, "US", None)):
            self.srv.anilist[mal] = country
            got = self.ask(f"mal:{mal}:1")
            self.assertEqual(origlang.choose(got)[0], lang, country)
        call = self.srv.of("anilist")[0]
        self.assertEqual(call["method"], "POST")
        body = json.loads(call["body"])
        self.assertEqual(body["variables"], {"id": 5114})
        self.assertIn("idMal", body["query"])
        self.assertIn("countryOfOrigin", body["query"])
        self.assertEqual(self.srv.of("tmdb") + self.srv.of("sparql"), [])     # no IMDb id: neither is asked

    def test_japanese_unless_anilist_says_otherwise(self):
        got = self.ask("mal:777")                                    # AniList doesn't know it
        self.assertEqual(origlang.choose(got), ("jpn", "rule"))
        self.assertEqual(got["failed"], [])
        self.srv.status["anilist"] = (500, ())
        got = self.ask("mal:778")
        self.assertEqual(origlang.choose(got), ("jpn", "rule"))
        self.assertEqual(got["failed"], ["anilist"])
        until = self.store.db.execute("SELECT until FROM title_language WHERE title='mal:778'").fetchone()[0]
        self.assertEqual(until, self.clock() + 600)

    def test_a_kitsu_id_goes_through_its_mal_number(self):
        self.srv.kitsu["1376"] = 5114
        self.srv.anilist[5114] = "JP"
        self.assertEqual(origlang.choose(self.ask("kitsu:1376:3")), ("jpn", "anilist"))
        (call,) = self.srv.of("kitsu")
        self.assertEqual(call["path"], "/kitsu/anime/1376")
        self.assertIn("include=mappings", call["query"])
        self.assertEqual(json.loads(self.srv.of("anilist")[0]["body"])["variables"], {"id": 5114})
        self.srv.kitsu["99"] = None                                  # no MyAnimeList mapping
        self.assertEqual(origlang.choose(self.ask("kitsu:99")), ("jpn", "rule"))
        self.assertEqual(len(self.srv.of("anilist")), 1)

    def test_anilist_is_asked_at_most_once_per_gap(self):
        self.lookup.anilist_gap = 0.3
        for mal in (11, 12, 13):
            self.srv.anilist[mal] = "JP"
        t0 = time.monotonic()
        for f in [self.lookup.start(f"mal:{m}", key="") for m in (11, 12, 13)]:
            f.result(timeout=10)
        self.assertGreaterEqual(time.monotonic() - t0, 0.6)


class KeepTest(Base):
    def until(self, title):
        return self.store.db.execute("SELECT until FROM title_language WHERE title=?", (title,)).fetchone()[0]

    def test_how_long_answers_are_kept(self):
        # a found answer is kept with no expiry, except one holding TMDB's value: TMDB's terms allow 6 months, so it is
        # kept 180 days and then looked up again in the background, as one not found is, and that play is timed as if
        # the language were unknown; Wikidata's and AniList's stay; not found and failed lookups keep their
        # short times, so they are asked again
        self.assertEqual(origlang.TMDB_KEEP_S, 180 * DAY)
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "original_language": "en"}]}
        self.srv.wikidata["tt2"] = [binding("Q1860", iso1="en")]
        self.srv.anilist[5] = "JP"
        self.ask("tt1")
        self.ask("tt2")
        self.ask("mal:5")
        self.assertEqual(self.until("tt1"), self.clock() + 180 * DAY)
        for title in ("tt2", "mal:5"):
            self.assertIsNone(self.until(title), title)
        tmdb_calls = lambda: len([c for c in self.srv.of("tmdb") if c["path"].endswith("/tt1")])  # noqa: E731
        self.clock.t += 180 * DAY - 1
        self.assertEqual(origlang.choose(self.lookup.peek("tt1")), ("eng", "tmdb"))
        self.assertIsNone(self.lookup.start("tt1", key=KEY))           # within its 6 months: not looked up again
        self.clock.t += 1
        self.assertIsNone(self.lookup.peek("tt1"))                     # expired: this play as if unknown
        self.clock.t += 20 * 365 * DAY
        self.store.expire()
        for vid in ("tt2", "mal:5"):
            self.assertIsNotNone(self.lookup.peek(vid), vid)
        self.assertIsNone(self.lookup.start("tt2", key=""))            # never looked up again
        self.assertEqual(tmdb_calls(), 1)
        self.assertEqual(origlang.choose(self.ask("tt1")), ("eng", "tmdb"))     # looked up again, kept 6 months more
        self.assertEqual(tmdb_calls(), 2)
        self.assertEqual(self.until("tt1"), self.clock() + 180 * DAY)

    def test_tmdbs_answers_kept_before_get_their_six_months(self):
        # An answer holding TMDB's value that an earlier version kept with no expiry gets one, counted from
        # when it was found, when the service starts; the others keep none
        found = self.clock() - 10 * DAY
        tmdb = {"tmdb": "eng", "tmdb_asked": True, "wikidata": ["eng"], "anime": None, "via": "tmdb", "failed": []}
        wikidata = dict(tmdb, tmdb=None, tmdb_asked=False, via="wikidata")
        none = dict(tmdb, tmdb="none", wikidata=[])
        old = dict(tmdb)
        for title, answer, at in (("tt1", tmdb, found), ("tt2", wikidata, found), ("tt3", none, found),
                                  ("tt4", old, self.clock() - 200 * DAY)):
            self.store.title_language_put(title, answer, at, None)
        self.store.db.execute("INSERT INTO title_language VALUES ('tt5', 'not json', ?, NULL)", (found,))
        self.make()
        self.assertEqual((self.until("tt1"), self.until("tt3")), (found + 180 * DAY, found + 180 * DAY))
        self.assertIsNone(self.until("tt2"))
        self.assertIsNone(self.until("tt5"))                           # unreadable: left as it was
        self.assertIsNotNone(self.lookup.peek("tt1"))
        self.assertIsNone(self.lookup.peek("tt4"))                     # past its 6 months: looked up again
        self.store.expire()
        self.assertEqual(sorted(r[0] for r in self.store.db.execute("SELECT title FROM title_language")),
                         ["tt1", "tt2", "tt3", "tt5"])

    def test_an_answer_is_shared_and_a_title_not_found_is_asked_again(self):
        self.srv.wikidata["tt2"] = [binding("Q1860", iso1="en")]
        self.ask("tt2")
        other = self.make()                                            # another worker, another person: one store
        self.assertEqual(other.peek("tt2")["wikidata"], ["eng"])
        self.assertIsNone(other.start("tt2", key=KEY))                 # nothing to ask
        self.ask("tt3")                                                # found nowhere: kept 7 days
        self.clock.t += 7 * DAY + 1
        self.assertIsNone(self.lookup.peek("tt3"))
        self.store.expire()
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT title FROM title_language")], ["tt2"])
        self.ask("tt3")
        self.assertEqual(len([c for c in self.srv.of("sparql") if "tt3" in c["query"]]), 2)

    def test_only_codes_are_stored(self):
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "title": "Inception", "overview": "A thief…",
                                                    "original_language": "en", "poster_path": "/x.jpg"}]}
        self.ask("tt1")
        (answer,) = self.store.db.execute("SELECT answer FROM title_language").fetchone()
        self.assertEqual(set(json.loads(answer)), {"tmdb", "tmdb_asked", "wikidata", "anime", "via", "failed"})
        self.assertNotIn("Inception", answer)

    def test_one_lookup_of_a_title_at_a_time_and_never_waiting(self):
        self.srv.delay["tmdb"] = 0.5
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "original_language": "en"}]}
        t0 = time.monotonic()
        first = self.lookup.start("tt1", key=KEY)
        second = self.lookup.start("tt1:1:1", key=KEY)
        self.assertLess(time.monotonic() - t0, 0.2)                   # returns at once
        self.assertIsNone(self.lookup.peek("tt1"))                    # nothing yet: the job goes on without it
        self.assertIs(second, first)
        first.result(timeout=10)
        self.assertEqual(len(self.srv.of("tmdb")), 1)
        self.assertEqual(self.lookup.peek("tt1")["tmdb"], "eng")

    def test_overrides_need_no_lookup(self):
        lk = self.make(SUBSYNC_ORIGINAL_LANGUAGES="tt1375666=en, tt2788316=ja,bad,tt3=zz")
        self.assertEqual(origlang.choose(lk.peek("tt2788316:1:1")), ("jpn", "override"))
        self.assertIsNone(lk.start("tt1375666", key=KEY))
        self.assertEqual(self.srv.calls, [])

    def test_anime_known_only_by_a_myanimelist_or_kitsu_number_can_be_set(self):
        # Only IMDb ids were taken, so the anime rule's weakest answers (kept with no expiry) couldn't be
        # corrected; an episode's id is still no key
        lk = self.make(SUBSYNC_ORIGINAL_LANGUAGES="mal:5114=ja,kitsu:7442=zh,mal:21:1=ko")
        self.assertEqual(origlang.choose(lk.peek("mal:5114:3")), ("jpn", "override"))
        self.assertEqual(origlang.choose(lk.peek("kitsu:7442")), ("chi", "override"))
        self.assertIsNone(lk.start("kitsu:7442:1", key=KEY))
        self.assertIsNone(lk.peek("mal:21"))
        self.assertEqual(self.srv.calls, [])

    def test_entries_left_out_are_counted_and_logged_once_at_start(self):
        self.assertEqual(origlang.parse_overrides("tt1375666=en, mal:5114=ja,bad,tt3=zz,mal:5114:3=ja,,kitsu:x=ja"),
                         ({"tt1375666": "eng", "mal:5114": "jpn"}, 4))
        with self.assertLogs("subsync.origlang", "WARNING") as logs:
            self.make(SUBSYNC_ORIGINAL_LANGUAGES="tt1375666=en, tt2788316=ja,bad,tt3=zz")
        self.assertEqual(logs.output, ["WARNING:subsync.origlang:SUBSYNC_ORIGINAL_LANGUAGES: 2 entries ignored "
                                       "(not an id=language pair)"])
        with self.assertNoLogs("subsync.origlang", "WARNING"):
            self.make(SUBSYNC_ORIGINAL_LANGUAGES="tt1375666=en,")

    def test_tmdb_answers_stay_when_tmdb_is_no_longer_used(self):
        self.srv.find["tt1"] = {"movie_results": [{"id": 1, "original_language": "en"}]}
        self.srv.wikidata["tt2"] = [binding("Q1860", iso1="en")]
        self.ask("tt1")
        self.ask("tt2", key="")
        lk = self.make(SUBSYNC_TMDB_KEY="")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM title_language").fetchone()[0], 2)
        self.assertEqual(lk.peek("tt1")["tmdb"], "eng")

    def test_switched_off_nothing_is_asked_and_the_answers_go(self):
        self.srv.wikidata["tt2"] = [binding("Q1860", iso1="en")]
        self.ask("tt2")
        self.assertIsNone(self.make(SUBSYNC_ORIGINAL_LANGUAGE="off"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM title_language").fetchone()[0], 0)


class SentTest(Base):
    def test_every_call_goes_through_the_gate_and_carries_only_the_id(self):
        # the gate's check (Real-Debrid refused, then the address rules) runs for every call: through
        # realdebrid.urlopen, and for TMDB's keyed calls through origlang's own opener that follows no redirect
        from subsync import realdebrid
        seen = []
        real = realdebrid.check

        def gate(url, *a, **kw):
            seen.append(url)
            return real(url, *a, **kw)
        self.srv.find["tt1375666"] = {"movie_results": [{"id": 27205, "original_language": "en"}]}
        self.srv.kitsu["1376"] = 5114
        self.srv.anilist[5114] = "JP"
        with mock.patch.object(realdebrid, "check", gate):
            self.ask("tt1375666:1:2")
            self.ask("kitsu:1376")
        self.assertEqual(len(seen), len(self.srv.calls))
        for c in self.srv.calls:
            text = c["path"] + "?" + urllib.parse.unquote(c["query"]) + c["body"]
            self.assertTrue(any(i in text for i in ("tt1375666", "1376", "5114")), text)
            for word in (":1:2", "mkv", "stremio", "manifest", "videoHash", "filename"):
                self.assertNotIn(word, text)

    def test_one_log_line_per_title(self):
        self.srv.find["tt2788316"] = {"tv_results": [{"id": 126308, "original_language": "en"}]}
        self.srv.wikidata["tt2788316"] = [binding("Q5287", iso1="ja")]
        with self.assertLogs("subsync.origlang", level="INFO") as cm:
            self.ask("tt2788316:1:1")
        lines = [r for r in cm.output if "original language" in r]
        self.assertEqual(len(lines), 1)
        self.assertIn("tt2788316: eng from TMDB", lines[0])
        self.assertIn("Wikidata: jpn", lines[0])                     # the disagreement, shown


if __name__ == "__main__":
    unittest.main()
