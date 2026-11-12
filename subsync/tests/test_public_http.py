"""Public mode over HTTP: the pages, the router in front of each person's own service, and the limits on requests."""
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import FakeAiostreams, TempDir  # noqa: E402
from subsync.settings import Settings  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
INSTANCE = "https://aios.test"
PASSWORD = "the-viewers-own-Password-1"
VIEWER = {"services": [{"id": "torbox", "enabled": True, "credentials": {"apiKey": "tb-key"}}],
          "presets": [{"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}},
                      {"type": "opensubtitles-v3-plus", "instanceId": "osp", "enabled": True,
                       "options": {"name": "OpenSubtitles V3+", "resources": ["subtitles"]}}]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def form(**fields):
    return urllib.parse.urlencode(fields).encode()


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class Base(unittest.TestCase):
    """A public service on this machine, with a stand-in AIOStreams; workers are not started (jobs stay queued)."""

    def start(self, **env):
        from subsync.aiosconfig import AiosApi
        from subsync.public import Pool, PublicService, serve_public
        from subsync.store import Store
        from subsync.users import Users
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.env = dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                         "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_EXTRA_SOURCES": ""}, **env)
        self.s = Settings(self.env)
        self.store = Store(self.dir)
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes())
        self.pool = Pool(self.s, self.env, self.store, self.users, start_workers=False)
        self.fake = FakeAiostreams()
        api = lambda base, timeout: AiosApi(base.replace(INSTANCE, self.fake.url), timeout=timeout,  # noqa: E731
                                            max_timeout=timeout, retry_wait_cap=5)
        self.service = PublicService(self.s, self.env, self.store, self.users, self.pool, api_factory=api)
        self.server = serve_public(self.service, 0, host="127.0.0.1")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.logs = Logs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.logs)
        self.server.shutdown()
        self.server.server_close()
        self.pool.close()
        self.fake.close()
        self.users.close()
        self.tmp.__exit__(None, None, None)

    def sign_up(self, **extra):
        uuid = self.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        fields = dict(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", password=PASSWORD,
                      languages="English", add_addon="on", add_opensubtitles="on")
        fields.update(extra)
        code, body, headers = http(self.base + "/signup", form(**fields))
        token = None
        text = body.decode()
        if code == 200:
            start = text.index("https://subs.test/u/") + len("https://subs.test/u/")
            token = text[start:start + 43]
        return uuid, code, text, headers, token


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class PublicHttpTest(Base):
    def setUp(self):
        self.start()

    # -- pages ------------------------------------------------------------------------------------------------------
    def test_signup_page_has_the_form_and_headers(self):
        code, body, headers = http(self.base + "/")
        text = body.decode()
        self.assertEqual(code, 200)
        for name in ('name="link"', 'name="password"', 'name="languages"', 'name="add_addon"', 'action="/signup"'):
            self.assertIn(name, text)
        self.assertNotIn('name="invite"', text)                      # no code set
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
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
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        # an error page repeats neither the link nor the password
        code, body, _ = http(self.base + "/signup", form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json",
                                                          password="wrong-" + PASSWORD, languages="English"))
        self.assertEqual(code, 400)
        self.assertNotIn(PASSWORD, body.decode())
        self.assertNotIn(f"stremio/{uuid}", body.decode())

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
        entries = json.loads(body)["subtitles"]                       # the "(Subsync)" entry of a file being worked on
        self.assertEqual([e["label"] for e in entries], ["English (Subsync)"])
        self.assertTrue(entries[0]["url"].startswith(f"https://subs.test/u/{token}/pick/"))
        self.assertEqual(len(self.pool.app(token).worker.jobs), 1)
        self.assertEqual(len(self.pool.app(other).worker.jobs), 0)

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
        uuid = self.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        link = f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json"
        self.assertEqual(http(self.base + "/signup", b"link=" + b"x" * 9000)[0], 413)
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
        self.assertIn("3 of 40", text)
        self.assertEqual(headers["Cache-Control"], "no-store")
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="languages", languages="Russian, en"))
        self.assertEqual(code, 200)
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
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="refresh", password=PASSWORD))
        text = body.decode()
        self.assertNotIn("Refreshed", text)
        self.assertIn("couldn", text)

    def test_refresh_over_http(self):
        uuid, _, _, _, token = self.sign_up()
        code, body, _ = http(f"{self.base}/u/{token}/configure", form(action="refresh", password=PASSWORD,
                                                                        add_addon="on"))
        self.assertEqual(code, 200, body)
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

    def test_no_secret_in_logs(self):
        uuid, _, _, _, token = self.sign_up()
        http(f"{self.base}/u/{token}/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        http(f"{self.base}/u/{token}/configure", form(action="delete", password=PASSWORD))
        for secret in (PASSWORD, token, f"stremio/{uuid}", self.users.user_id(token)):
            for line in self.logs.lines:
                self.assertNotIn(secret, line)

    def test_forwarded_for_only_from_the_trusted_proxy(self):
        handler = self.service

        class Request:
            def __init__(self, peer, forwarded):
                self.client_address = (peer, 1234)
                self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        self.assertEqual(handler.client_ip(Request("127.0.0.1", "8.8.8.8, 1.2.3.4")), "1.2.3.4")
        self.assertEqual(handler.client_ip(Request("127.0.0.1", None)), "127.0.0.1")
        self.assertEqual(handler.client_ip(Request("9.9.9.9", "1.2.3.4")), "9.9.9.9")   # not the proxy: ignored

    def test_signup_attempts_are_limited_per_address(self):
        for _ in range(5):
            self.assertEqual(http(self.base + "/signup", form(link="", password="x"))[0], 400)
        code, body, _ = http(self.base + "/signup", form(link="", password="x"))
        self.assertEqual(code, 429)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class ConnectionCapTest(Base):
    def setUp(self):
        self.start(SUBSYNC_MAX_CONNECTIONS="1")

    def test_connection_cap(self):
        held = socket.create_connection(("127.0.0.1", self.server.server_address[1]))
        try:
            time.sleep(0.2)                                   # the first connection holds the only slot
            t0 = time.time()
            code, _, headers = http(self.base + "/health")
            self.assertEqual(code, 503)
            self.assertLess(time.time() - t0, 2)
        finally:
            held.close()
        time.sleep(0.2)
        self.assertEqual(http(self.base + "/health")[0], 200)


if __name__ == "__main__":
    unittest.main()
