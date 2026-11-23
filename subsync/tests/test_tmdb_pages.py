"""TMDB on the public pages: its logo and notice wherever the instance uses TMDB, the privacy page's words on the
original-language lookups, and (when the instance asks each person for one) a person's own TMDB key on their settings
page, checked with TMDB once when saved, kept only in their encrypted record and never shown."""
import html as htmllib
import json
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

# helpers first: it puts the package on the path
from helpers import (form, HAVE_CRYPTOGRAPHY, http, LINK, NEEDS, RECORD, ServerHarness, settings, start_server,
                     stop_server, USAGE)
from subsync import explain, origlang, pages, ui

PERSON_KEY = "fedcba9876543210fedcba9876543210"


def notice_in(html):
    return explain.TMDB_NOTICE in html and 'class="tmdb-logo"' in html


class AttributionTest(unittest.TestCase):
    def pages_of(self, s):
        return {"start": pages.signup_page(s), "settings": pages.settings_page(s, RECORD, USAGE, LINK, now=0),
                "privacy": pages.privacy_page(s), "deleted": pages.deleted_page({"sync_removed": True, "addon_removed": True}),
                "not found": pages.not_found_page()}

    def test_shown_wherever_tmdb_is_used(self):
        for env in ({"SUBSYNC_TMDB_KEY": "k"}, {"SUBSYNC_TMDB_PERSONAL": "on"}):
            for name, html in self.pages_of(settings(**env)).items():
                if name in ("deleted", "not found"):
                    continue                     # pages made without the instance's settings carry no footer
                with self.subTest(env=env, page=name):
                    self.assertTrue(notice_in(html))

    def test_not_shown_without_tmdb(self):
        for env in ({}, {"SUBSYNC_TMDB_KEY": "k", "SUBSYNC_ORIGINAL_LANGUAGE": "off"}):
            for name, html in self.pages_of(settings(**env)).items():
                with self.subTest(env=env, page=name):
                    self.assertNotIn(explain.TMDB_NOTICE, html)
                    self.assertNotIn("tmdb-logo", html)

    def test_the_notice_and_the_logo(self):
        self.assertEqual(explain.TMDB_NOTICE, "AIOSubtitles uses TMDB and the TMDB APIs but is not endorsed, certified, "
                                              "or otherwise approved by TMDB.")
        credit = ui.tmdb_credit(explain.TMDB_NOTICE)
        self.assertIn("<svg", credit)
        self.assertIn('role="img"', credit)
        self.assertIn('aria-label="TMDB"', credit)
        self.assertNotIn("<style", credit)               # the pages' security header allows no inline style
        self.assertNotIn("style=", credit)
        self.assertNotIn("<img", credit)

    def test_the_addons_description_carries_it_when_a_key_is_set(self):
        from subsync.addon import manifest
        self.assertIn(explain.TMDB_NOTICE, manifest(settings(SUBSYNC_TMDB_KEY="k"))["description"])
        self.assertNotIn("TMDB", manifest(settings())["description"])

    def test_each_addons_description_says_what_the_instance_does(self):
        # "or its speech" only while the audio route is on, TMDB's notice while TMDB is used: the public
        # addon's too (below), and the self-hosted one no longer says speech with its audio route off
        from subsync.addon import MANIFEST, manifest
        from subsync.settings import Settings
        own = Settings({"SUBSYNC_AUDIO_ROUTE": "off"})
        self.assertEqual(manifest(own)["description"],
                         "Synced copies of the subtitles a viewer sees, checked against the release's own subtitle "
                         "track; below the bar the original is kept.")
        self.assertEqual(manifest(Settings({"SUBSYNC_AUDIO_ROUTE": "on"}))["description"],
                         "Synced copies of the subtitles a viewer sees, checked against the release's own subtitle "
                         "track or its speech; below the bar the original is kept.")
        self.assertEqual({k: v for k, v in manifest(own).items() if k != "description"},
                         {k: v for k, v in MANIFEST.items() if k != "description"})

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_public_addons_description_says_what_the_instance_does(self):
        #'s public half, on its own: public mode needs the cryptography package
        from subsync.addon import manifest
        from subsync.public import PUBLIC_MANIFEST
        public = "Synced copies of the subtitles you see, checked against each file's own subtitle track{}; below " \
                 "the bar the original is kept."
        notice = (" AIOSubtitles uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by "
                  "TMDB.")
        for env, description in (({"SUBSYNC_AUDIO_ROUTE": "on"}, public.format(" or its speech")),
                                 ({"SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_TMDB_PERSONAL": "on"},
                                  public.format(" or its speech") + notice),
                                 ({"SUBSYNC_TMDB_KEY": "k"}, public.format("") + notice),
                                 ({}, public.format(""))):
            with self.subTest(env=env):
                made = manifest(settings(**env), PUBLIC_MANIFEST)
                self.assertEqual(made["description"], description)
                self.assertEqual({k: v for k, v in made.items() if k != "description"},
                                 {k: v for k, v in PUBLIC_MANIFEST.items() if k != "description"})


class PrivacyTest(unittest.TestCase):
    def read_part(self, s):
        from subsync.extra import asked_sources
        return dict(explain.privacy_parts(s, asked_sources(s, "series")))

    def test_the_lookup_sentence_by_the_instances_setting(self):
        cases = (({"SUBSYNC_TMDB_KEY": "k"}, explain.PRIVACY_LOOKUP["tmdb"]),
                 ({"SUBSYNC_TMDB_PERSONAL": "on"}, explain.PRIVACY_LOOKUP["personal"]),
                 ({}, explain.PRIVACY_LOOKUP["wikidata"]),
                 ({"SUBSYNC_ORIGINAL_LANGUAGE": "off"}, explain.PRIVACY_LOOKUP["off"]))
        for env, sentence in cases:
            with self.subTest(env=env):
                read = self.read_part(settings(**env))["What is read"]
                self.assertIn(sentence, read)
                self.assertIn("Cinemeta", read)
        self.assertIn("at TMDB, Wikidata", explain.PRIVACY_LOOKUP["tmdb"])
        self.assertIn("with your own key if you added one", explain.PRIVACY_LOOKUP["personal"])
        self.assertNotIn("TMDB", explain.PRIVACY_LOOKUP["wikidata"] + explain.PRIVACY_LOOKUP["off"])

    def test_the_stored_part_names_a_persons_key_only_when_asked_for(self):
        self.assertIn("your TMDB key if you added one,", self.read_part(settings(SUBSYNC_TMDB_PERSONAL="on"))["What is stored"])
        self.assertNotIn("TMDB", self.read_part(settings(SUBSYNC_TMDB_KEY="k"))["What is stored"])

    def test_with_the_audio_route_too(self):
        read = self.read_part(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_TMDB_KEY="k"))["What is read"]
        self.assertIn(explain.PRIVACY_LOOKUP["tmdb"], read)
        self.assertIn("short parts of its sound", read)


class SettingsRowTest(unittest.TestCase):
    def test_only_when_each_person_may_add_one(self):
        on = pages.settings_page(settings(SUBSYNC_TMDB_PERSONAL="on"), RECORD, USAGE, LINK, now=0)
        # The box is required (the form only saves a key), so its label no longer says "(optional)"; the
        # row's explanation says a key is
        self.assertIn('<label for="tmdb_key">Your TMDB key</label>', on)
        self.assertNotIn("Your TMDB key (optional)", on)
        self.assertRegex(on, r'<input class="input mono" id="tmdb_key" name="tmdb_key" type="password" [^>]* required>')
        self.assertIn('name="tmdb_key"', on)
        self.assertIn('type="password"', on)
        self.assertIn('value="tmdb-key"', on)                          # the action
        self.assertIn("Save key", on)
        self.assertNotIn('value="tmdb-key-remove"', on)                # nothing to remove yet
        for env in ({}, {"SUBSYNC_TMDB_KEY": "k"}):
            self.assertNotIn("Your TMDB key", pages.settings_page(settings(**env), RECORD, USAGE, LINK, now=0))

    def test_a_kept_key_is_never_shown(self):
        page = pages.settings_page(settings(SUBSYNC_TMDB_PERSONAL="on"), dict(RECORD, tmdb_key=PERSON_KEY), USAGE, LINK,
                                   now=0)
        self.assertNotIn(PERSON_KEY, page)
        self.assertIn('value="tmdb-key-remove"', page)
        self.assertIn(">Remove key<", page)

    def test_a_kept_key_can_still_be_removed_once_the_instance_stops_asking(self):
        for env in ({"SUBSYNC_TMDB_KEY": "k"}, {"SUBSYNC_TMDB_PERSONAL": "on", "SUBSYNC_ORIGINAL_LANGUAGE": "off"}, {}):
            page = pages.settings_page(settings(**env), dict(RECORD, tmdb_key=PERSON_KEY), USAGE, LINK, now=0)
            with self.subTest(env=env):
                self.assertIn('value="tmdb-key-remove"', page)
                self.assertNotIn('name="tmdb_key"', page)               # nothing to save a new one with
                self.assertNotIn(PERSON_KEY, page)

    def test_the_done_messages(self):
        self.assertEqual(pages.done_message("tmdb-key", RECORD), (explain.PUBLIC_TEXTS["tmdb_saved"], None))
        self.assertEqual(pages.done_message("tmdb-key-removed", RECORD), (explain.PUBLIC_TEXTS["tmdb_removed"], None))


class FakeTmdb:
    """Answers /configuration: 200 for the one good key, 401 for any other; or nothing at all."""

    def __init__(self):
        self.calls, self.down = [], False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                outer.calls.append((url.path, url.query, self.headers.get("authorization")))
                key = urllib.parse.parse_qs(url.query).get("api_key", [""])[0]
                here = url.path == "/3/configuration"
                ok = here and key == PERSON_KEY
                body = json.dumps({"images": {}} if ok else {"status_code": 7 if here else 34}).encode()
                self.send_response(200 if ok else 401 if here else 404)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.srv = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/3"

    def close(self):
        stop_server(self.srv)


class KeyCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmdb = FakeTmdb()
        self.addCleanup(self.tmdb.close)

    def test_one_call_says_whether_tmdb_takes_the_key(self):
        s = settings(SUBSYNC_TMDB_PERSONAL="on", SUBSYNC_TMDB_URL=self.tmdb.url)
        self.assertIs(origlang.check_key(s, PERSON_KEY), True)
        self.assertIs(origlang.check_key(s, "0" * 32), False)
        self.assertEqual(len(self.tmdb.calls), 2)
        self.assertEqual(self.tmdb.calls[0][0], "/3/configuration")
        self.assertIsNone(origlang.check_key(settings(SUBSYNC_TMDB_URL="http://127.0.0.1:9/3"), PERSON_KEY))
        wrong = settings(SUBSYNC_TMDB_PERSONAL="on", SUBSYNC_TMDB_URL=self.tmdb.url + "/x")
        self.assertIsNone(origlang.check_key(wrong, PERSON_KEY))       # a wrong address: not a refused key


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PersonsKeyTest(unittest.TestCase):
    """The settings page's "tmdb-key" and "tmdb-key-remove" actions, end to end through public mode's server."""

    def setUp(self):
        self.tmdb = FakeTmdb()
        self.addCleanup(self.tmdb.close)
        self.case = ServerHarness()
        self.case.start(SUBSYNC_TMDB_PERSONAL="on", SUBSYNC_TMDB_URL=self.tmdb.url, SUBSYNC_ORIGINAL_LANGUAGE="on",
                        SUBSYNC_WIKIDATA_URL="http://127.0.0.1:9/sparql")
        self.addCleanup(self.case.close)

    def post(self, token, **fields):
        return http(f"{self.case.base}/u/{token}/configure", form(**fields))

    def test_saved_when_tmdb_takes_it_used_and_removed(self):
        c = self.case
        _, _, _, _, token = c.sign_up()
        code, body, _ = self.post(token, action="tmdb-key", tmdb_key="0" * 32)
        self.assertEqual(code, 400)
        self.assertIn(htmllib.escape(explain.PUBLIC_TEXTS["tmdb_refused"]), body.decode())
        self.assertNotIn("tmdb_key", c.users.open(token))
        code, _, headers = self.post(token, action="tmdb-key", tmdb_key=f"  {PERSON_KEY} ")
        self.assertEqual((code, headers["Location"]), (303, "configure?done=tmdb-key"))
        self.assertEqual(c.users.open(token)["tmdb_key"], PERSON_KEY)
        self.assertEqual(c.pool.app(token).s.tmdb_key, PERSON_KEY)       # their lookups use it from now on
        raw = c.users.db.execute("SELECT blob FROM users").fetchone()[0]
        self.assertNotIn(PERSON_KEY.encode(), bytes(raw))                 # encrypted in the record
        code, body, _ = http(f"{c.base}/u/{token}/configure")
        self.assertNotIn(PERSON_KEY, body.decode())
        code, _, headers = self.post(token, action="tmdb-key-remove")
        self.assertEqual((code, headers["Location"]), (303, "configure?done=tmdb-key-removed"))
        self.assertNotIn("tmdb_key", c.users.open(token))
        self.assertEqual(c.pool.app(token).s.tmdb_key, "")

    def test_tmdb_not_answering(self):
        c = self.case
        _, _, _, _, token = c.sign_up()
        self.tmdb.close()
        code, body, _ = self.post(token, action="tmdb-key", tmdb_key=PERSON_KEY)
        self.assertEqual(code, 400)
        self.assertIn(htmllib.escape(explain.PUBLIC_TEXTS["tmdb_unreachable"]), body.decode())
        self.assertNotIn("tmdb_key", c.users.open(token))

    def test_closing_the_service_closes_its_lookups(self):
        # what is checked here is that the service closes its one Lookup, whose pool then takes no more lookups; that
        # closing it drops the lookups still queued is test_origlang's KeySafetyTest (this read a private flag
        # of ThreadPoolExecutor instead)
        lookup = self.case.pool.lookup
        with mock.patch.object(lookup, "close", wraps=lookup.close) as close:
            self.case.pool.close()
        close.assert_called_once_with()
        with self.assertRaises(RuntimeError):            # "cannot schedule new futures after shutdown"
            lookup.pool.submit(lambda: None)

    def test_refused_when_the_instance_doesnt_ask(self):
        self.case.tearDown()
        self.case.start(SUBSYNC_TMDB_KEY="instance", SUBSYNC_TMDB_URL=self.tmdb.url, SUBSYNC_ORIGINAL_LANGUAGE="on",
                        SUBSYNC_WIKIDATA_URL="http://127.0.0.1:9/sparql")
        _, _, _, _, token = self.case.sign_up()
        code, _, _ = self.post(token, action="tmdb-key", tmdb_key=PERSON_KEY)
        self.assertEqual(code, 400)
        self.assertEqual(self.tmdb.calls, [])
        self.assertEqual(self.case.pool.app(token).s.tmdb_key, "instance")
        with self.case.users.locked(token):                            # a key saved while the instance still asked
            record = self.case.users.open(token)
            record["tmdb_key"] = PERSON_KEY
            self.case.users.update(token, record)
        code, _, headers = self.post(token, action="tmdb-key-remove")
        self.assertEqual((code, headers["Location"]), (303, "configure?done=tmdb-key-removed"))
        self.assertNotIn("tmdb_key", self.case.users.open(token))


if __name__ == "__main__":
    unittest.main()
