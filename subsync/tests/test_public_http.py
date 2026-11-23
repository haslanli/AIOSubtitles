"""Public mode over HTTP: the pages, the router in front of each person's own service, and the limits on requests."""
import json
import logging
import socket
import time
import unittest

from helpers import (answers, exchange, form, HAVE_CRYPTOGRAPHY, http, INSTANCE, NEEDS, PASSWORD,
                     ServerHarness, VIEWER, wait_until)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PublicManifestTest(ServerHarness, unittest.TestCase):
    def test_the_addons_description_says_speech_while_the_audio_route_is_on(self):
        # Built from the instance's settings
        self.start(SUBSYNC_AUDIO_ROUTE="on")
        _, _, _, _, token = self.sign_up()
        code, body, _ = http(f"{self.base}/u/{token}/manifest.json")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["description"], "Synced copies of the subtitles you see, checked against "
                                                          "each file's own subtitle track or its speech; below the bar "
                                                          "the original is kept.")

    def test_the_addons_version_is_the_releases(self):
        # It said 1.0.0 whatever the release (AIOStreams shows it)
        from subsync.version import VERSION
        self.start()
        _, _, _, _, token = self.sign_up()
        code, body, headers = http(f"{self.base}/u/{token}/manifest.json")
        self.assertEqual((code, json.loads(body)["version"]), (200, VERSION))
        self.assertEqual(headers["Cache-Control"], "max-age=3600")       # as the self-hosted one's
        self.assertNotEqual(VERSION, "1.0.0")

    def test_a_head_of_a_list_or_an_entry_starts_nothing(self):
        # A HEAD ran the whole GET: the person's service was started for it, their daily check too, and the
        # list, pick, pending and "(original)" routes did their work; now they answer 200 with their content type
        from subsync.addon import token as entry_token
        from subsync.users import new_token
        self.start(SUBSYNC_RECHECK_HOURS="24")
        _, _, _, _, token = self.sign_up()
        read_key, tok = self.users.read_key(token), entry_token("name:a.mkv|5")
        srt = "application/x-subrip; charset=utf-8"
        port = self.server.server_address[1]

        def head(path):
            data = exchange(port, f"HEAD {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            return data.partition(b"\r\n\r\n")
        for path, ctype in ((f"/u/{token}/subtitles/movie/tt1/filename=a.mkv&videoSize=5.json",
                             "application/json; charset=utf-8"), (f"/r/{read_key}/pick/{tok}/eng.srt", srt),
                            (f"/r/{read_key}/pending/{tok}/eng.srt", srt),
                            (f"/r/{read_key}/original/{tok}/eng.srt", srt)):
            with self.subTest(path=path):
                headers, _, rest = head(path)
                self.assertTrue(headers.startswith(b"HTTP/1.1 200 "), headers)
                self.assertIn(f"\r\nContent-Type: {ctype}\r\n".encode(), headers + b"\r\n")
                self.assertNotIn(b"Content-Length", headers)
                self.assertEqual(rest, b"")
        self.assertIsNone(self.pool.live_app(token))                    # no service of theirs started
        self.assertEqual(self.service.checker.next, {})                 # nor their daily check
        unknown = head(f"/u/{new_token()}/subtitles/movie/tt1/filename=a.mkv&videoSize=5.json")[0]
        self.assertTrue(unknown.startswith(b"HTTP/1.1 404 "), unknown)   # a link nobody signed up with, as a GET

    def test_a_head_of_an_address_its_get_refuses_is_not_found_and_starts_nothing(self):
        # A HEAD answered 200 before the checks its GET makes (an id that names no title, an entry token that
        # names no file), where the GET is "not found"
        self.start(SUBSYNC_RECHECK_HOURS="24")
        _, _, _, _, token = self.sign_up()
        read_key = self.users.read_key(token)
        port = self.server.server_address[1]

        def status(method, path):
            data = exchange(port, f"{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            return int(data.split(b" ", 2)[1])
        refused = (f"/u/{token}/subtitles/movie/{'t' * 300}.json", f"/u/{token}/subtitles/movie/tt1%0A.json",
                   f"/r/{read_key}/pick/A/eng.srt", f"/r/{read_key}/pending/_w/eng.srt",
                   f"/r/{read_key}/original/A/eng.srt")
        for path in refused:
            with self.subTest(path=path):
                self.assertEqual(status("HEAD", path), 404)
        self.assertIsNone(self.pool.live_app(token))                    # no service of theirs started
        self.assertEqual(self.service.checker.next, {})                 # nor their daily check
        for path in refused:
            with self.subTest(path=path):
                self.assertEqual(status("GET", path), 404)              # as the GET answers


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PublicHttpTest(ServerHarness, unittest.TestCase):
    def setUp(self):
        self.start()

    # -- pages ------------------------------------------------------------------------------------------------------
    def test_signup_page_has_the_form_and_headers(self):
        code, body, headers = http(self.base + "/")
        text = body.decode()
        self.assertEqual(code, 200)
        for name in ('name="link"', 'name="password"', 'name="lang"', 'name="add_addon"', 'action="/signup"'):
            self.assertIn(name, text)
        self.assertNotIn('name="invite"', text)                      # no code set
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store, no-transform")
        self.assertNotIn("http://", text.replace("http://www.w3.org", ""))   # no outside resources
        self.assertEqual(http(self.base + "/privacy")[0], 200)
        self.assertEqual(http(self.base + "/health")[:2], (200, b"ok"))

    def test_unknown_token_is_404(self):
        from subsync.users import new_token
        for path in (f"/u/{new_token()}/manifest.json", f"/u/{new_token()}/configure", "/u/short/manifest.json",
                     f"/u/{new_token()}/subtitles/movie/tt1.json", "/nothing", "/u/"):
            with self.subTest(path=path):
                self.assertEqual(http(self.base + path)[0], 404)

    def test_signup_round_trip_over_http(self):
        uuid, code, text, headers, token = self.sign_up()
        self.assertEqual(code, 200, text)
        self.assertEqual(text.count(f"https://subs.test/u/{token}/manifest.json"), 1)    # the link, shown once
        self.assertNotIn(PASSWORD, text)
        self.assertEqual(headers["Cache-Control"], "no-store, no-transform")
        self.assertIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        # an error page repeats neither the link nor the password
        code, body, _ = http(self.base + "/signup", form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json",
                                                          password="wrong-" + PASSWORD, languages="English"))
        self.assertEqual(code, 400)
        self.assertNotIn(PASSWORD, body.decode())
        self.assertNotIn(f"stremio/{uuid}", body.decode())

    def test_signup_with_an_alias_link_over_http(self):
        uuid = self.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        self.fake.aliases["Home-TV"] = uuid
        code, body, _ = http(self.base + "/signup", form(link=f"{INSTANCE}/stremio/u/Home-TV/manifest.json",
                                                          password=PASSWORD, languages="English", add_addon="on"))
        self.assertEqual(code, 200, body.decode())
        self.assertIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        # an unknown alias: the plain text at the link field, and the page repeats neither the alias nor the password
        code, body, _ = http(self.base + "/signup", form(link=f"{INSTANCE}/stremio/u/Old-Name/manifest.json",
                                                          password=PASSWORD, languages="English"))
        text = body.decode()
        self.assertEqual(code, 400)
        self.assertIn("Your AIOStreams doesn&#x27;t know this alias link", text)
        self.assertIn('href="#link"', text)
        for secret in ("Old-Name", "Home-TV", PASSWORD):
            self.assertNotIn(secret, text)

    def test_user_manifest(self):
        _, _, _, _, token = self.sign_up()
        code, body, _ = http(f"{self.base}/u/{token}/manifest.json")
        m = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual((m["id"], m["name"], m["resources"]), ("org.aiosubtitles.public", "AIOSubtitles", ["subtitles"]))
        self.assertTrue(m["behaviorHints"]["configurable"])

    def test_user_subtitles_reach_their_worker(self):
        _, _, _, _, token = self.sign_up()
        _, _, _, _, other = self.sign_up()
        code, body, _ = http(f"{self.base}/u/{token}/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        self.assertEqual(code, 200)
        entries = json.loads(body)["subtitles"]       # the pick entry of a file being worked on, named for the public
        self.assertEqual([e["label"] for e in entries], ["English (AIOSubtitles)"])   # service
        # Under their read key, never their personal link
        self.assertTrue(entries[0]["url"].startswith(f"https://subs.test/r/{self.users.read_key(token)}/pick/"))
        self.assertEqual(len(self.pool.app(token).worker.jobs), 1)
        self.assertEqual(len(self.pool.app(other).worker.jobs), 0)

    def test_subtitle_entries_carry_a_read_key_that_opens_them_only(self):
        # Every entry an app gets carried the personal link's token, which opens the settings page (delete,
        # Refresh with a new link, the addon's address, any title's subtitles); now a read key opens the entries only
        from subsync.users import Users
        _, _, _, _, token = self.sign_up()
        key = self.users.read_key(token)
        code, body, _ = http(f"{self.base}/u/{token}/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        self.assertEqual(code, 200)
        self.set_state(token, "sync_gone")            # a notice entry too
        code, body, _ = http(f"{self.base}/u/{token}/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        urls = [e["url"] for e in json.loads(body)["subtitles"]]
        self.assertIn(f"https://subs.test/r/{key}/notice.srt", urls)
        self.assertTrue([u for u in urls if "/pick/" in u], urls)
        for url in urls:
            self.assertTrue(url.startswith(f"https://subs.test/r/{key}/"), url)
            self.assertNotIn(token, url)
        self.assertEqual(http(f"{self.base}/r/{key}/notice.srt")[0], 200)      # the key serves its entries
        for path in ("/configure", "/manifest.json", "/status", "/subtitles/movie/tt1.json", "/", ""):
            with self.subTest(path=path):
                self.assertEqual(http(f"{self.base}/r/{key}{path}")[0], 404)
        code, _, _ = http(f"{self.base}/r/{key}/configure", form(action="delete"))
        self.assertEqual(code, 405)
        self.assertIsNotNone(self.users.open(token))                           # nothing deleted
        self.assertEqual(http(f"{self.base}/u/{token}/notice.srt")[0], 404)   # entries no longer under the token
        other = Users(self.dir + "/other.sqlite3", b"\x01" * 32)
        try:
            changed = key[:10] + ("A" if key[10] != "A" else "B") + key[11:]     # one character of it changed
            for made_up in ("A" * 79, other.read_key(token), changed, token):
                with self.subTest(key=made_up):
                    self.assertEqual(http(f"{self.base}/r/{made_up}/notice.srt")[0], 404)
        finally:
            other.close()

    def set_state(self, token, state):
        record = self.users.open(token)
        record["state"] = state
        self.users.update(token, record)
        self.pool.stop(token)

    def test_shared_files_are_served_to_anyone(self):
        from subsync import ALGO_VERSION
        key = self.store.put("0123456789abcdef-1000", "a" * 64, "eng", 0, "synced", body="1\n00:00:01,000 --> "
                             "00:00:02,000\nHi\n", fmt="srt")
        fid = key.split(":", 1)[1].replace(":", "_")
        self.assertTrue(fid.startswith(ALGO_VERSION))
        code, body, _ = http(f"{self.base}/file/{fid}.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"Hi", body)

    def test_odd_form_input(self):
        from subsync.public import MAX_FORM
        uuid = self.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        link = f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json"
        for size, code in ((MAX_FORM, 400), (MAX_FORM + 1, 413)):          # read up to the limit, refused past it
            with self.subTest(size=size):
                self.assertEqual(http(self.base + "/signup", b"link=" + b"x" * (size - len(b"link=")))[0], code)
        code, body, _ = http(self.base + "/signup", b"link=\xff\xfe&password=x")
        self.assertEqual(code, 400)
        for fields in (dict(link=link, password="a\x00b", languages="English"),
                       dict(link=link, password=PASSWORD, languages="en, ru, de, fr, es"),
                       dict(link=link, password=PASSWORD, languages="Klingon")):
            with self.subTest(fields=fields["languages"]):
                code, body, _ = http(self.base + "/signup", form(**fields))
                self.assertEqual(code, 400)
                self.assertNotIn(f"stremio/{uuid}", body.decode())
                self.assertNotIn(PASSWORD, body.decode())
        self.assertEqual(self.fake.writes(), [])

    def test_settings_page_shows_usage_and_changes_languages(self):
        _, _, _, _, token = self.sign_up()
        self.users.add_usage(token, files=3, nbytes=5 * 1024 * 1024)
        code, body, headers = http(f"{self.base}/u/{token}/configure")
        text = body.decode()
        self.assertEqual(code, 200)
        self.assertIn("3 of 200", text)                  # 200 new files a day
        self.assertEqual(headers["Cache-Control"], "no-store, no-transform")
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="languages", languages="Russian, en"))
        self.assertEqual(code, 303)                      # saved, then the page again, so a reload sends nothing
        self.assertEqual(self.users.open(token)["languages"], ["rus", "eng"])
        self.assertEqual(self.pool.app(token).s.languages, ["rus", "eng"])     # the worker uses them from now on

    def test_status_is_not_served_to_people(self):
        _, _, _, _, token = self.sign_up()
        self.assertEqual(http(f"{self.base}/u/{token}/status")[0], 404)

    def test_a_refresh_that_could_not_add_the_addon_says_so(self):
        uuid, _, _, _, token = self.sign_up()
        main = self.fake.configs[uuid]["config"]
        main["presets"] = [p for p in main["presets"] if p["options"].get("name") != "AIOSubtitles"]
        self.fake.refuse_save_for.add(uuid)                     # the sync config saves; the person's own doesn't
        code, _, headers = http(f"{self.base}/u/{token}/configure", form(action="refresh", password=PASSWORD))
        self.assertEqual((code, headers["Location"]), (303, "configure?done=refreshed-manual"))
        text = http(f"{self.base}/u/{token}/configure?done=refreshed-manual")[1].decode()
        self.assertNotIn("Refreshed", text)
        self.assertIn("couldn", text)

    def test_a_refresh_whose_change_was_sent_but_not_checked_says_so(self):
        # A save sent but not checked (no answer, a failed read-back) was told as one that changed nothing
        import html
        from unittest import mock
        from subsync import explain, signup
        _, _, _, _, token = self.sign_up()
        with mock.patch.object(signup, "_add_addon", return_value=(["adds 1 subtitle source"], False, False, True)):
            code, _, headers = http(f"{self.base}/u/{token}/configure", form(action="refresh", password=PASSWORD))
        self.assertEqual((code, headers["Location"]), (303, "configure?done=refreshed-unchecked"))
        text = html.unescape(http(f"{self.base}/u/{token}/configure?done=refreshed-unchecked")[1].decode())
        self.assertIn(explain.PUBLIC_REFRESH_UNCHECKED + " " + explain.PUBLIC_MANUAL_STEPS[0], text)
        self.assertNotIn(explain.PUBLIC_TEXTS["refresh_manual"], text)

    def test_refresh_over_http(self):
        uuid, _, _, _, token = self.sign_up()
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="refresh", password=PASSWORD,
                                                                        add_addon="on"))
        self.assertEqual(code, 303, body)
        self.assertNotIn(PASSWORD, body.decode())

    def test_delete_flow(self):
        uuid, _, _, _, token = self.sign_up()
        sync_uuid = self.users.open(token)["sync_uuid"]
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="delete-confirm"))
        self.assertEqual(code, 200)
        self.assertIn('value="delete"', body.decode())
        self.assertIn(sync_uuid, self.fake.configs)                         # nothing deleted yet
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="delete", password=PASSWORD))
        self.assertEqual(code, 200)
        self.assertNotIn(sync_uuid, self.fake.configs)
        self.assertNotIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        self.assertEqual(http(f"{self.base}/u/{token}/manifest.json")[0], 404)
        self.assertEqual(http(f"{self.base}/u/{token}/configure")[0], 404)

    def test_every_page_links_to_the_hosts_own_contact(self):
        # SUBSYNC_CONTACT is the pages' Questions link, the pages the router makes without a person included;
        # without it they show none
        def questions(text):
            return f'<a href="{contact}">Questions</a>' in text
        contact = "mailto:host@example.com"
        self.assertFalse(questions(http(self.base + "/privacy")[1].decode()))
        self.assertNotIn(">Questions<", http(self.base + "/")[1].decode())
        self.close()
        self.start(SUBSYNC_CONTACT=contact)
        uuid, code, text, headers, token = self.sign_up()
        self.assertTrue(questions(text))                                       # the signed-up page
        for page in (http(self.base + "/")[1], http(self.base + "/privacy")[1],
                     http(self.base + "/nothing", headers={"Accept": "text/html"})[1],
                     http(f"{self.base}/u/{token}/configure")[1],
                     http(f"{self.base}/u/{token}/configure", form(action="delete-confirm"))[1],
                     http(f"{self.base}/u/{token}/configure", form(action="delete", password=PASSWORD))[1]):
            with self.subTest(page=page[:200]):
                self.assertTrue(questions(page.decode()))
        self.assertIn(f'<p><a class="mono" href="{contact}">{contact}</a></p>', http(self.base + "/privacy")[1].decode())

    def test_no_secret_in_logs(self):
        uuid, _, _, _, token = self.sign_up()
        http(f"{self.base}/u/{token}/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        http(f"{self.base}/u/{token}/configure", form(action="delete", password=PASSWORD))
        for secret in (PASSWORD, token, f"stremio/{uuid}", self.users.user_id(token)):
            for line in self.logs.lines:
                self.assertNotIn(secret, line)

    def test_the_log_lines_read_hold_a_logged_exceptions_traceback(self):
        # Only each record's message was kept, so a secret in a logged exception never reached the check above
        try:
            raise ValueError("held by the exception only")
        except ValueError:
            logging.getLogger("subsync.public").warning("request failed", exc_info=True)
        self.assertIn("ValueError: held by the exception only", self.logs.lines[-1])

    def test_forwarded_for_only_from_the_trusted_proxy(self):
        handler = self.service

        class Request:
            def __init__(self, peer, forwarded):
                self.client_address = (peer, 1234)
                self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        self.assertEqual(handler.client_ip(Request("127.0.0.1", "8.8.8.8, 1.2.3.4")), "1.2.3.4")
        self.assertEqual(handler.client_ip(Request("127.0.0.1", None)), "127.0.0.1")
        self.assertEqual(handler.client_ip(Request("9.9.9.9", "1.2.3.4")), "9.9.9.9")   # not the proxy: ignored

    def test_a_body_no_route_reads_closes_the_connection(self):
        # The body stayed on the kept-open connection and was read as the next request: a request hidden in it
        # ran as if the web server in front had sent it (its X-Forwarded-For believed), and behind a web server that
        # keeps its connections its answer went to whoever that server sent next
        port = self.server.server_address[1]
        hidden = b"GET /health HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 203.0.113.9\r\nConnection: close\r\n\r\n"
        for line, code in ((b"POST /health", 405), (b"POST /nope", 404), (b"GET /static/app.css", 200),
                           (b"POST /u/" + b"A" * 43 + b"/manifest.json", 405)):
            with self.subTest(line=line):
                data = exchange(port, line + b" HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(hidden)
                                + hidden)
                self.assertEqual(answers(data), [code])
                self.assertIn(b"\r\nConnection: close\r\n", data)
        # a form the route reads keeps the connection: the next request on it is answered
        body = form(link="", password="x")
        data = exchange(port, b"POST /signup HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                        b"Content-Length: %d\r\n\r\n" % len(body) + body + hidden)
        self.assertEqual(answers(data), [400, 200])
        # But not when its length is given twice: the form read by the first, a front end may have sent the
        # rest as part of it
        data = exchange(port, b"POST /signup HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                        b"Content-Length: %d\r\nContent-Length: %d\r\n\r\n" % (len(body), len(body) + len(hidden))
                        + body + hidden)
        self.assertEqual(answers(data), [400])
        self.assertIn(b"\r\nConnection: close\r\n", data)

    def test_a_form_without_its_length_is_refused_and_closes_the_connection(self):
        # A POST without Content-Length (411) had no test
        port = self.server.server_address[1]
        hidden = b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        data = exchange(port, b"POST /signup HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                              b"Accept: text/html\r\n\r\nlink=&password=x" + hidden)
        self.assertEqual(answers(data), [411])                 # and closed: what followed is never read
        self.assertIn(b"The form arrived without its length.", data)

    def test_a_length_not_in_digits_alone_is_no_length(self):
        # The form's Content-Length was read with int, which takes a sign and underscores: "+16" and "1_6"
        # read the form, "-1" was called too large. Such a length is none, as every number in a header is read
        # (digits.py, and parse_request); spaces around one are not part of it
        port = self.server.server_address[1]
        body = form(link="", password="x")
        n = str(len(body))
        for length, code in (("+" + n, 411), (n[0] + "_" + n[1:], 411), ("-1", 411), (f" {n} ", 400)):
            with self.subTest(length=length):
                data = exchange(port, b"POST /signup HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded"
                                b"\r\nConnection: close\r\nContent-Length: " + length.encode() + b"\r\n\r\n" + body)
                self.assertEqual(answers(data), [code])

    def test_a_405_names_the_methods_the_address_takes(self):
        # RFC 9110 asks a 405 to name them (Allow); none was named
        _, _, _, _, token = self.sign_up()
        port = self.server.server_address[1]
        for path in ("/health", "/file/x", f"/r/{self.users.read_key(token)}/notice.srt", f"/u/{token}/manifest.json"):
            with self.subTest(path=path):
                data = exchange(port, f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n"
                                      "\r\n".encode())
                head = data.partition(b"\r\n\r\n")[0]
                self.assertTrue(head.startswith(b"HTTP/1.1 405 "), head)
                self.assertIn(b"\r\nAllow: GET, HEAD\r\n", head + b"\r\n")

    def test_a_request_that_trickles_in_is_cut_off(self):
        # The timeout bounds each wait, not the request: a byte at a time held a connection, and one of
        # SUBSYNC_MAX_CONNECTIONS, for hours
        from unittest import mock
        from subsync import public
        port = self.server.server_address[1]
        signup = b"POST /signup HTTP/1.1\r\nHost: x\r\nContent-Length: 30\r\n\r\n"
        for head, rest, answer in ((signup, b"link=&password=" + b"x" * 15, [500]),        # the form
                                   (b"GET /health HTTP/1.1\r\n", b"X-Slow: " + b"a" * 21 + b"\r\n\r\n", [])):  # the head
            with self.subTest(head=head.split(b" ")[0]), mock.patch.object(public, "REQUEST_ARRIVAL_S", 0.5):
                seconds, data = trickle(port, head, rest, 0.1)
                self.assertLess(seconds, 2.0)                    # not the 3 s the whole request took to come
                self.assertEqual(answers(data), answer)
        self.assertEqual(http(self.base + "/health")[:2], (200, b"ok"))
        seconds, data = trickle(port, b"GET /health HTTP/1.1\r\n", b"Host: x\r\n\r\n", 0.05)   # slow, but in time
        self.assertEqual(answers(data), [200])

    def test_the_stylesheet_and_script_are_kept_for_good_at_their_hashed_address(self):
        # Pages ask for them by their content's hash, yet they were kept an hour
        from subsync import ui
        for url in (ui.CSS_URL, ui.JS_URL):
            path = url.split("?")[0]
            with self.subTest(path=path):
                code, _, headers = http(self.base + url)
                self.assertEqual((code, headers["Cache-Control"]), (200, "public, max-age=31536000, immutable"))
                for other in (path, path + "?v=0000000000", url + "&v=1"):    # an old page's, or none
                    code, _, headers = http(self.base + other)
                    self.assertEqual((code, headers["Cache-Control"]), (200, "public, max-age=3600"))

    def test_a_store_of_recipes_serves_no_copy_at_the_root(self):
        # Public mode's store keeps how each copy was made, and a copy is rebuilt from a person's own list
        # under their link; the root /file/ route still read the recipe and the result, then answered 404
        from unittest import mock
        key = self.store.put("0123456789abcdef-1000", "a" * 64, "eng", 0, "synced", body="1\n00:00:01,000 --> "
                             "00:00:02,000\nHi\n", fmt="srt")
        fid = key.split(":", 1)[1].replace(":", "_")
        self.store.keep_texts = False                     # as public mode keeps its store (__main__.public_store)
        with mock.patch.object(self.store, "recipe") as recipe, mock.patch.object(self.store, "get") as get:
            self.assertEqual(http(f"{self.base}/file/{fid}.srt")[:2], (404, b""))
        recipe.assert_not_called()
        get.assert_not_called()
        self.assertEqual(http(self.base + "/health")[:2], (200, b"ok"))

    def test_a_delete_with_a_mistyped_password_leaves_the_persons_service_as_it_was(self):
        # Their App was stopped with forget before the password was checked: a mistyped one deleted nothing
        # ("Nothing was deleted"), yet their queued picks, the files the copies in their lists need and their refusal
        # back-off were dropped, and the job running stored nothing
        _, _, _, _, token = self.sign_up()
        self.assertEqual(http(f"{self.base}/u/{token}/subtitles/movie/tt1.json")[0], 200)   # their App
        app, uid = self.pool.live_app(token), self.users.user_id(token)
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="delete", password="not-the-password"))
        self.assertEqual(code, 400)
        self.assertIn(b"Nothing was deleted", body)
        self.assertIs(self.pool.live_app(token), app)
        self.assertFalse(app.worker.forgotten or app.worker.stop_flag)
        self.assertIn(uid, self.pool.safeties)
        code, _, _ = http(f"{self.base}/u/{token}/configure", form(action="delete", password=PASSWORD))
        self.assertEqual(code, 200)
        self.assertTrue(app.worker.forgotten)
        self.assertIsNone(self.pool.live_app(token))
        self.assertNotIn(uid, self.pool.safeties)

    def test_a_request_during_the_delete_leaves_nothing_serving(self):
        # The delete's AIOStreams calls take seconds; a subtitle request then made the person's App again,
        # and it went on serving the deleted link
        from unittest import mock
        from subsync import public
        _, _, _, _, token = self.sign_up()
        real = public.delete

        def delete(*a, **k):
            self.assertEqual(http(f"{self.base}/u/{token}/subtitles/movie/tt1.json")[0], 200)   # their app, meanwhile
            return real(*a, **k)
        with mock.patch.object(public, "delete", delete):
            code, _, _ = http(f"{self.base}/u/{token}/configure", form(action="delete", password=PASSWORD))
        self.assertEqual(code, 200)
        self.assertIsNone(self.pool.live_app(token))
        self.assertEqual(http(f"{self.base}/u/{token}/subtitles/movie/tt1.json")[0], 404)


def trickle(port, head, rest, gap):
    """Sends `head` at once, then `rest` a byte every `gap` seconds until the server answers or closes the connection.
    Returns the seconds until it did, and what it sent."""
    import select
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head)
        for byte in rest:
            if select.select([sock], [], [], gap)[0]:
                break
            try:
                sock.sendall(bytes([byte]))
            except OSError:                           # closed already
                break
        select.select([sock], [], [], 10)
        seconds = time.monotonic() - t0
        sock.shutdown(socket.SHUT_WR)
        data = b""
        try:
            while chunk := sock.recv(65536):
                data += chunk
        except OSError:
            pass
    return seconds, data


class ServerTest(unittest.TestCase):
    """Both modes' HTTP server (addon.Server): connections that come together wait to be taken, and one
    that broke between requests is no failure: no traceback."""

    def check(self, server):
        import contextlib
        import io
        self.addCleanup(server.server_close)
        self.assertGreaterEqual(server.request_queue_size, 128)

        def handled(error):
            printed = io.StringIO()
            with contextlib.redirect_stderr(printed):
                try:
                    raise error
                except Exception:
                    server.handle_error(None, ("127.0.0.1", 1))
            return printed.getvalue()
        with self.assertNoLogs("subsync.addon", "WARNING"):
            for error in (ConnectionResetError(54, "Connection reset by peer"), BrokenPipeError(32, "Broken pipe"),
                          TimeoutError("timed out")):
                self.assertEqual(handled(error), "")
        with self.assertLogs("subsync.addon", "WARNING") as logs:
            self.assertEqual(handled(ValueError("odd")), "")
        self.assertEqual(logs.output, ["WARNING:subsync.addon:connection failed: ValueError"])

    def test_the_self_hosted_server(self):
        from unittest import mock
        from subsync import addon
        self.check(addon.Server(("127.0.0.1", 0), addon.Handler))
        with mock.patch.object(addon, "Server") as made:
            addon.serve(None, 8080)
        self.assertEqual(made.call_args.args[0], ("0.0.0.0", 8080))
        with mock.patch.object(addon, "Server") as made:                  # SUBSYNC_BIND
            addon.serve(None, 8080, "127.0.0.1")
        self.assertEqual(made.call_args.args[0], ("127.0.0.1", 8080))

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_public_server(self):
        from subsync.public import PublicHandler, _CappedServer
        self.check(_CappedServer(("127.0.0.1", 0), PublicHandler, 4))


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class ConnectionCapTest(ServerHarness, unittest.TestCase):
    def setUp(self):
        self.start(SUBSYNC_MAX_CONNECTIONS="1")

    def test_connection_cap(self):
        held = socket.create_connection(("127.0.0.1", self.server.server_address[1]))

        def refused():
            t0 = time.monotonic()
            code = http(self.base + "/health")[0]
            self.assertLess(time.monotonic() - t0, 2)               # answered at once, never left waiting for the slot
            return code == 503
        try:
            self.assertTrue(wait_until(refused, timeout=10))   # once the server has the first connection, its only slot
        finally:
            held.close()
        self.assertTrue(wait_until(lambda: http(self.base + "/health")[0] == 200, timeout=10))   # the slot is free again


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PublicCheckTest(ServerHarness, unittest.TestCase):
    """The daily check as people see it: a note on the settings page, one "action needed" entry in their subtitle
    lists while only they can fix something, the kept-password box, and Refresh with a new link."""

    def setUp(self):
        self.start()

    def set_record(self, token, **fields):
        record = self.users.open(token)
        record.update(fields)
        self.users.update(token, record)
        self.pool.stop(token)                 # the next request opens the record again

    def subtitles(self, token):
        code, body, _ = http(f"{self.base}/u/{token}/subtitles/movie/tt1.json")
        self.assertEqual(code, 200)
        return json.loads(body)["subtitles"]

    def notices(self, token):
        return [s for s in self.subtitles(token) if s["id"] == "aiosubtitles-notice"]

    def test_one_notice_entry_while_an_action_is_needed(self):
        from subsync import explain
        uuid, code, text, headers, token = self.sign_up()
        self.assertEqual(self.notices(token), [])
        key = self.users.read_key(token)                               # entries are under the read key
        code, body, _ = http(f"{self.base}/r/{key}/notice.srt")       # an entry kept by an app after the fix
        self.assertEqual(code, 200)
        self.assertIn(explain.PUBLIC_NOTICE_DONE, body.decode())
        self.set_record(token, state="sync_gone")
        notes = self.notices(token)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["label"], "AIOSubtitles: action needed")
        self.assertEqual(notes[0]["lang"], "eng")
        self.assertEqual(notes[0]["url"], f"https://subs.test/r/{key}/notice.srt")
        code, body, headers = http(f"{self.base}/r/{key}/notice.srt")
        self.assertEqual(code, 200)
        self.assertIn(explain.PUBLIC_STATES["sync_gone"][1][:40], " ".join(body.decode().split()))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.set_record(token, state="unreachable")       # a note on the settings page only
        self.assertEqual(self.notices(token), [])
        self.set_record(token, state=None)
        self.assertEqual(self.notices(token), [])

    def test_reads_refused_three_times_needs_an_action(self):
        uuid, code, text, headers, token = self.sign_up()
        self.pool.app(token).safety.refusals = 3
        self.assertEqual(len(self.notices(token)), 1)
        page = http(f"{self.base}/u/{token}/configure")[1].decode()
        from subsync import explain
        import html
        self.assertIn(html.escape(explain.PUBLIC_STATES["reads_refused"][0]), page)

    def test_the_settings_page_shows_the_state_and_when_the_config_was_copied(self):
        from subsync import explain
        uuid, code, text, headers, token = self.sign_up()
        page = http(f"{self.base}/u/{token}/configure")[1].decode()
        self.assertIn("copied from yours today", page)
        self.set_record(token, state="main_gone")
        page = http(f"{self.base}/u/{token}/configure")[1].decode()
        self.assertIn(explain.PUBLIC_STATES["main_gone"][0], page)
        self.assertIn('name="link"', page)                  # Refresh takes the new link

    def test_the_kept_password_box_and_forgetting_it(self):
        # the box is offered only while the check runs; a fresh record's check is a day away, so none runs
        self.close()
        self.start(SUBSYNC_RECHECK_HOURS="24")
        uuid, code, text, headers, token = self.sign_up()
        self.assertNotIn("auto_password", self.users.open(token))
        signup_page = http(self.base + "/")[1].decode()
        self.assertIn('name="keep_password"', signup_page)
        self.assertNotIn('name="keep_password" value="on" checked', signup_page)    # unticked by default
        uuid, code, text, headers, token = self.sign_up(keep_password="on")
        self.assertEqual(self.users.open(token)["auto_password"], PASSWORD)
        page = http(f"{self.base}/u/{token}/configure")[1].decode()
        self.assertIn('value="forget-password"', page)
        code, _, headers = http(f"{self.base}/u/{token}/configure", form(action="forget-password"))
        self.assertEqual(code, 303)
        self.assertNotIn("auto_password", self.users.open(token))
        page = http(f"{self.base}/u/{token}/configure?done=forgot")[1].decode()
        self.assertNotIn('value="forget-password"', page)

    def test_delete_removes_the_persons_own_rows_in_the_store_at_once(self):
        # The files a person waited to pick and their original subtitles stayed in the shared store for up to
        # 7 days after "Your data is deleted"; a delete that doesn't go through (a mistyped password) removes nothing
        from subsync.users import new_token
        from subsync.store import REFUSED_BACKOFF_S
        uuid, code, text, headers, token = self.sign_up()
        mine, other = self.users.rows_key(token), self.users.rows_key(new_token())
        for key in (mine, other):
            self.store.pickable_put(f"p:{key}:f1", "movie", "tt1", {"filename": "a.mkv", "videoSize": 5})
            self.store.originals_put(f"p:{key}:f1", {"eng": {"url": "https://subs.example/1.srt", "id": "1"}})
            self.store.file_failed(f"p:{key}:f2", REFUSED_BACKOFF_S)

        def left(key):
            return (self.store.pickable_get(f"p:{key}:f1") is not None,
                    self.store.originals_get(f"p:{key}:f1") is not None, self.store.file_blocked(f"p:{key}:f2"))
        code, _, _ = http(f"{self.base}/u/{token}/configure", form(action="delete", password="not-the-password"))
        self.assertEqual(code, 400)
        self.assertEqual(left(mine), (True, True, True))
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="delete"))
        self.assertEqual(code, 200)
        self.assertIsNone(self.users.open(token))
        self.assertEqual(left(mine), (False, False, False))
        self.assertEqual(left(other), (True, True, True))

    def test_with_the_check_off_no_password_is_offered_or_kept(self):
        # This harness runs with SUBSYNC_RECHECK_HOURS 0; a kept password would never be used
        self.assertNotIn('name="keep_password"', http(self.base + "/")[1].decode())
        uuid, code, text, headers, token = self.sign_up(keep_password="on")       # sent anyway: not kept
        self.assertEqual(code, 200)
        self.assertNotIn("auto_password", self.users.open(token))
        self.set_record(token, auto_password=PASSWORD)                         # kept before the check was turned off
        page = http(f"{self.base}/u/{token}/configure")[1].decode()
        self.assertIn('value="forget-password"', page)                         # it can still be deleted at once
        self.assertNotIn('name="keep_password"', page)
        self.assertNotIn("follows changes to your config by itself", page)
        code, _, _ = http(f"{self.base}/u/{token}/configure", form(action="refresh", password=PASSWORD,
                                                                     keep_password="on"))
        self.assertEqual(code, 303)
        self.assertNotIn("auto_password", self.users.open(token))              # the next Refresh deletes it

    def test_refresh_with_a_new_link_over_http(self):
        uuid, code, text, headers, token = self.sign_up()
        new_main = self.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        code, _, headers = http(f"{self.base}/u/{token}/configure",
                                form(action="refresh", password=PASSWORD,
                                     link=f"{INSTANCE}/stremio/{new_main}/ENCRYPTED/manifest.json"))
        self.assertEqual(code, 303, headers)
        self.assertEqual(self.users.open(token)["main_uuid"], new_main)

    def test_a_request_starts_the_check_in_the_background(self):
        self.close()
        self.start(SUBSYNC_RECHECK_HOURS="24")
        uuid, code, text, headers, token = self.sign_up()
        record = self.users.open(token)
        sync_uuid = record["sync_uuid"]
        record.pop("checked_at")
        record["next_check"] = 0                      # a record from before the check existed: due at once
        self.users.update(token, record)
        heads = sum(1 for m, _ in self.fake.requests if m == "HEAD")
        self.subtitles(token)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.users.open(token).get("checked_at"):
            time.sleep(0.05)
        self.assertIsNotNone(self.users.open(token).get("checked_at"))
        self.assertEqual(sum(1 for m, _ in self.fake.requests if m == "HEAD"), heads + 1)
        self.assertEqual(self.users.open(token)["sync_uuid"], sync_uuid)
        self.service.checker.join(5)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class AudioChoiceHttpTest(ServerHarness, unittest.TestCase):
    """over HTTP: the answer for files with no subtitles of their own, chosen at sign-up, shown and changed on
    the settings page with the "audio" action, read back by the person's worker; nothing of it on an instance with
    the audio route off."""

    def setUp(self):
        self.start(SUBSYNC_AUDIO_ROUTE="on")

    def page(self, token, query=""):
        code, body, _ = http(f"{self.base}/u/{token}/configure{query}")
        self.assertEqual(code, 200)
        return body.decode()

    def test_chosen_at_sign_up_then_changed_on_the_settings_page(self):
        import html
        from subsync import explain
        code, body, _ = http(self.base + "/")
        self.assertIn('name="audio" id="audio-pick" value="pick" checked', body.decode())
        _, code, text, _, token = self.sign_up(audio="open")
        self.assertEqual(code, 200, text)
        self.assertEqual(self.users.open(token)["audio"], "open")
        self.assertEqual(self.pool.app(token).s.audio_choice, "open")
        self.assertIn('name="audio" id="audio-open" value="open" checked', self.page(token))
        code, _, headers = http(f"{self.base}/u/{token}/configure", form(action="audio", audio="off"))
        self.assertEqual((code, headers["Location"]), (303, "configure?done=audio"))
        self.assertEqual(self.users.open(token)["audio"], "off")
        text = html.unescape(self.page(token, "?done=audio"))
        self.assertIn(explain.PUBLIC_TEXTS["audio_saved"].format(answer="Off"), text)
        self.assertIn('name="audio" id="audio-off" value="off" checked', text)
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="audio", audio="loud"))
        self.assertEqual(code, 400)
        self.assertIn("Choose one of the answers for files with no subtitles of their own.", body.decode())
        self.assertIn('href="#audio"', body.decode())
        self.assertEqual(self.users.open(token)["audio"], "off")

    def test_an_answer_not_offered_at_sign_up_is_refused_before_anything_is_made(self):
        uuid, code, text, _, token = self.sign_up(audio="loud")
        self.assertEqual(code, 400)
        self.assertIsNone(token)
        self.assertIn("Choose one of the answers for files with no subtitles of their own.", text)
        self.assertIn('data-open-at="link"', text)       # the first emptied screen, its summary links on
        self.assertIn('href="#audio"', text)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.users.count(), 0)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class AudioOffHttpTest(ServerHarness, unittest.TestCase):
    def setUp(self):
        self.start()

    def test_no_answer_asked_shown_or_saved_with_the_audio_route_off(self):
        self.assertNotIn('name="audio"', http(self.base + "/")[1].decode())
        _, code, text, _, token = self.sign_up(audio="open")
        self.assertEqual(code, 200, text)
        self.assertNotIn("audio", self.users.open(token))
        self.assertNotIn('name="audio"', http(f"{self.base}/u/{token}/configure")[1].decode())
        code, _, _ = http(f"{self.base}/u/{token}/configure", form(action="audio", audio="pick"))
        self.assertEqual(code, 400)
        self.assertNotIn("audio", self.users.open(token))


if __name__ == "__main__":
    unittest.main()
