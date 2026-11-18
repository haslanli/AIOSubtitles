"""Public mode's sign-up, refresh and delete, against a stand-in AIOStreams on this machine. The password is used once
and never stored or logged; Real-Debrid is warned about and left out, or, when it is the only service, the sign-up is
for stored copies only."""
import copy
import logging
import os
import time
import unittest

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
TORRENTIO = {"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}}
OSV3 = {"type": "opensubtitles-v3-plus", "instanceId": "osp", "enabled": True,
        "options": {"name": "OpenSubtitles V3+", "resources": ["subtitles"]}}
NEWZNAB = {"type": "newznab", "instanceId": "nzb", "enabled": True, "options": {"name": "Indexer"}}
TORBOX = {"id": "torbox", "enabled": True, "credentials": {"apiKey": "tb-key"}}
REALDEBRID = {"id": "realdebrid", "enabled": True, "credentials": {"apiKey": "rd-key"}}
AIOS_USENET = {"id": "aiostreams", "enabled": True, "credentials": {}}   # AIOStreams' built-in Usenet engine
PROWLARR = {"type": "prowlarr", "instanceId": "prw", "enabled": True,   # its own Services choice names that engine
            "options": {"name": "Prowlarr", "services": ["torbox", "aiostreams"]}}
TESTER_ERROR = ("You have specified AIOStreams in your configuration for Prowlarr, but it is not enabled or has "
                "missing credentials")   # what AIOStreams told a tester at sign-up
# the texts for alias links and a config AIOStreams refuses, as approved
UNKNOWN_ALIAS = ("Your AIOStreams doesn't know this alias link: its alias may have been changed or removed. Copy the "
                 "install link again from AIOStreams' configure page.")
NO_CONFIG_FROM_ALIAS = ("AIOSubtitles couldn't get your config's full link from this alias link. Open the alias link "
                        "in your browser: AIOStreams takes you to the full link, which has /stremio/ and a long code in "
                        "it. Copy that address and paste it here.")
ALIAS_UNREACHABLE = ("Opening your alias link: AIOSubtitles can't reach your AIOStreams from the internet. Check the "
                     "link, and that your AIOStreams can be opened from outside your home network.")
REFUSED_CONFIG = ("Your AIOStreams didn't accept the config AIOSubtitles makes from yours. This is a fault in "
                  "AIOSubtitles, not in your config, and your own config wasn't changed. AIOStreams' reason: \"{}\"")


def viewer(services=(TORBOX,), presets=(TORRENTIO, OSV3, NEWZNAB)):
    return {"services": [dict(s) for s in services], "presets": [dict(p) for p in presets],
            "failover": {"enabled": True}, "preloadStreams": {"enabled": True}}


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class SignupTest(unittest.TestCase):
    def setUp(self):
        from subsync import signup
        from subsync.aiosconfig import AiosApi
        from subsync.users import Users
        self.signup = signup
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                           "SUBSYNC_PUBLIC_BASE": "https://subs.test"})
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes())
        self.fake = FakeAiostreams()
        self.api = lambda base, timeout: AiosApi(base.replace(INSTANCE, self.fake.url), timeout=timeout,
                                                 max_timeout=timeout, retry_wait_cap=5)
        self.logs = Logs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.logs)
        self.fake.close()
        self.users.close()
        self.tmp.__exit__(None, None, None)

    def form(self, uuid, **extra):
        return dict({"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                     "languages": "English", "add_addon": "on"}, **extra)

    def run_signup(self, config, timeout=10, **extra):
        uuid = self.fake.add(config, PASSWORD)
        return uuid, self.signup.signup(self.s, self.users, self.form(uuid, **extra), api_factory=self.api,
                                        timeout=timeout)

    def sync_config(self, record):
        return self.fake.configs[record["sync_uuid"]]["config"]

    def assert_no_secret_anywhere(self, *secrets):
        self.users.close()
        data = b"".join(open(os.path.join(self.dir, f), "rb").read() for f in os.listdir(self.dir)
                        if f.startswith("users.sqlite3"))
        from subsync.users import Users
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes())
        for secret in secrets:
            self.assertNotIn(secret.encode(), data)
            for line in self.logs.lines:
                self.assertNotIn(secret, line)

    # -- sign-up -------------------------------------------------------------------------------------------------
    def test_signup_with_torbox(self):
        uuid, result = self.run_signup(viewer())
        self.assertEqual(result.manifest_url, f"https://subs.test/u/{result.token}/manifest.json")
        self.assertIsNone(result.warning)
        self.assertTrue(result.added)
        self.assertFalse(result.manual)
        record = self.users.open(result.token)
        self.assertEqual(record["languages"], ["eng"])
        self.assertEqual(record["main_uuid"], uuid)
        sync = self.sync_config(record)
        self.assertEqual([p["type"] for p in sync["presets"]], ["torrentio", "opensubtitles-v3-plus"])   # no Usenet
        self.assertFalse(sync["failover"]["enabled"])
        self.assertFalse(sync["preloadStreams"]["enabled"])
        self.assertTrue(record["sync_link"].startswith(f"{INSTANCE}/stremio/{record['sync_uuid']}/"))
        main = self.fake.configs[uuid]["config"]
        names = [p["options"]["name"] for p in main["presets"]]
        self.assertEqual(names, ["Torrentio", "AIOSubtitles", "OpenSubtitles V3+", "Indexer"])
        self.assertEqual(main["presets"][1]["options"]["manifestUrl"], result.manifest_url)
        self.assert_no_secret_anywhere(PASSWORD, f"/stremio/{uuid}/ENCRYPTED", result.token)

    def test_real_debrid_with_torbox_warns_and_leaves_it_out(self):
        from subsync import explain
        _, result = self.run_signup(viewer(services=(TORBOX, REALDEBRID)))
        self.assertEqual(result.warning, explain.PUBLIC_REAL_DEBRID_WARNING)
        sync = self.sync_config(self.users.open(result.token))
        self.assertEqual([s["id"] for s in sync["services"]], ["torbox"])

    def test_real_debrid_only_signs_up_for_stored_copies_and_creates_nothing(self):
        _, result = self.run_signup(viewer(services=(REALDEBRID,)), add_addon="")   # more in test_stored_copies
        self.assertTrue(result.store_only)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.users.count(), 1)

    def test_usenet_only_stops(self):
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(viewer(services=(), presets=(NEWZNAB, OSV3)))
        self.assertIn("debrid", e.exception.message)
        self.assertEqual(self.fake.writes(), [])

    def test_http_link_is_refused(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        form = self.form(uuid, link=f"http://aios.test/stremio/{uuid}/ENCRYPTED/manifest.json")
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.signup(self.s, self.users, form, api_factory=self.api)
        self.assertIn("https", e.exception.message)
        self.assertEqual(self.fake.requests, [])

    def test_links_that_are_not_aiostreams_links(self):
        for link in ("", "https://aios.test/", "https://aios.test/stremio/not-a-uuid/E/manifest.json", "ftp://x/y",
                     "https://aios.test/stremio/" + "1" * 36 + "/E/manifest.json?x=1", "x" * 3000):
            with self.subTest(link=link[:40]):
                with self.assertRaises(self.signup.SignupError):
                    self.signup.parse_link(link)
        uuid = "1a2b3c4d-1111-2222-3333-444455556666"
        self.assertEqual(self.signup.parse_link(f" https://aio.example.com/stremio/{uuid}/abc/manifest.json "),
                         ("https://aio.example.com", uuid, None))
        self.assertEqual(self.signup.parse_link(f"https://example.com/aio/stremio/{uuid.upper()}/abc/manifest.json"),
                         ("https://example.com/aio", uuid, None))

    # -- alias links and variants ------------------------------------------------------------------
    def test_link_shapes(self):
        uuid = "1a2b3c4d-1111-2222-3333-444455556666"
        base = "https://aio.example.com"
        for link, parts in (
                (f"{base}/stremio/u/Family.TV/manifest.json", (base, None, "Family.TV")),   # case kept (instance-wide)
                ("https://example.com/aio/stremio/u/a%20b/manifest.json", ("https://example.com/aio", None, "a b")),
                (f"{base}/stremio/u/fam/v/kids,tv/manifest.json", (base, None, "fam")),
                (f"{base}/stremio/u/fam/manifest.json?v=kids", (base, None, "fam")),
                (f"{base}/stremio/{uuid}/abc/v/kids/manifest.json", (base, uuid, None)),
                (f"{base}/stremio/{uuid}/abc/manifest.json?v=kids%2Ctv", (base, uuid, None))):
            with self.subTest(link=link):
                self.assertEqual(self.signup.parse_link(link), parts)
        for link in (f"{base}/stremio/u/manifest.json", f"{base}/stremio/u//manifest.json", f"{base}/stremio/u/fam",
                     f"{base}/stremio/u/fam/configure", f"{base}/stremio/u/fam/v/a/b/manifest.json",
                     f"{base}/stremio/{uuid}/abc/manifest.json?v=kids&x=1", f"{base}/stremio/{uuid}/abc/manifest.json?x=1",
                     f"{base}/stremio/{uuid}/abc/manifest.json?v=", f"{base}/stremio/{uuid}/abc/v/manifest.json",
                     f"{base}/stremio/{uuid}/abc/v//manifest.json", f"{base}/stremio/{uuid}/abc/w/kids/manifest.json"):
            with self.subTest(link=link):
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup.parse_link(link)
                self.assertEqual(e.exception.field, "link")

    def test_an_overlong_alias_is_refused_without_asking(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.signup(self.s, self.users, dict(self.form(uuid), link=f"{INSTANCE}/stremio/u/{'a' * 201}/"
                                                                               "manifest.json"), api_factory=self.api)
        self.assertEqual((e.exception.message, e.exception.field), (UNKNOWN_ALIAS, "link"))
        self.assertEqual(self.fake.requests, [])

    def test_signup_with_an_alias_link_follows_the_config_behind_it(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        self.fake.aliases["Family.TV"] = uuid
        self.fake.encrypted[uuid] = "SECRETencrypted42"
        form = dict(self.form(uuid), link=f"{INSTANCE}/stremio/u/Family.TV/manifest.json")
        result = self.signup.signup(self.s, self.users, form, api_factory=self.api)
        record = self.users.open(result.token)
        self.assertEqual((record["instance"], record["main_uuid"]), (INSTANCE, uuid))
        self.assertEqual(self.fake.requests[0], ("GET", "/stremio/u/Family.TV/manifest.json"))
        self.assertEqual(self.fake.alias_auth, [False])                     # asked without any password
        self.assertFalse([p for _, p in self.fake.requests if "SECRETencrypted42" in p])   # the redirect not followed
        self.assertTrue(result.added)
        self.assertIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        self.assert_no_secret_anywhere("SECRETencrypted42", "Family.TV", f"{uuid}/SECRET")
        import json
        kept = json.dumps(self.users.open(result.token))                  # the decrypted record itself
        self.assertNotIn("SECRETencrypted42", kept)
        self.assertNotIn("Family.TV", kept)

    def test_a_redirect_whose_body_never_comes_still_gives_the_config(self):
        import socket
        import threading
        from subsync.aiosconfig import AiosApi
        uuid = "1a2b3c4d-1111-2222-3333-444455556666"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        done = threading.Event()

        def stall():   # the headers of a redirect, then a body that is promised and never sent (held open)
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.sendall(f"HTTP/1.1 302 Found\r\nLocation: /stremio/{uuid}/SECRET/manifest.json\r\n"
                         "Content-Length: 100000\r\n\r\nFound".encode())
            done.wait(10)
            conn.close()
        threading.Thread(target=stall, daemon=True).start()
        api = AiosApi(f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=2, max_timeout=2)
        try:
            self.assertEqual(api.alias_config_id("fam"), uuid)
        finally:
            done.set()
            listener.close()

    def test_an_alias_link_with_a_variant(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        self.fake.aliases["fam"] = uuid
        form = dict(self.form(uuid), link=f"{INSTANCE}/stremio/u/fam/v/kids/manifest.json")
        result = self.signup.signup(self.s, self.users, form, api_factory=self.api)
        self.assertEqual(self.users.open(result.token)["main_uuid"], uuid)
        self.assertEqual(self.fake.requests[0], ("GET", "/stremio/u/fam/manifest.json"))

    def test_an_absolute_redirect_on_the_same_host(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        self.fake.aliases["fam"], self.fake.alias_answer = uuid, "same_host"
        result = self.signup.signup(self.s, self.users, dict(self.form(uuid), link=f"{INSTANCE}/stremio/u/fam/"
                                                                                   "manifest.json"), api_factory=self.api)
        self.assertEqual(self.users.open(result.token)["main_uuid"], uuid)

    def test_an_unknown_alias(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        before = copy.deepcopy(self.fake.configs)
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.signup(self.s, self.users, dict(self.form(uuid), link=f"{INSTANCE}/stremio/u/nobody/"
                                                                               "manifest.json"), api_factory=self.api)
        self.assertEqual((e.exception.message, e.exception.field), (UNKNOWN_ALIAS, "link"))
        self.assertEqual(self.fake.requests, [("GET", "/stremio/u/nobody/manifest.json")])
        self.assertEqual(self.fake.configs, before)
        self.assertEqual(self.users.count(), 0)

    def test_an_alias_lookup_that_gives_no_config(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        self.fake.aliases["home"] = uuid
        for answer in ("ok_200", "other_host", "no_id"):
            with self.subTest(answer=answer):
                self.fake.requests.clear()
                self.fake.alias_answer = answer
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup.signup(self.s, self.users, dict(self.form(uuid), link=f"{INSTANCE}/stremio/u/home/"
                                                                                       "manifest.json"),
                                       api_factory=self.api)
                self.assertEqual((e.exception.message, e.exception.field), (NO_CONFIG_FROM_ALIAS, "link"))
                self.assertEqual(self.fake.requests, [("GET", "/stremio/u/home/manifest.json")])
                self.assertEqual(len(self.fake.configs), 1)
                self.assertEqual(self.users.count(), 0)

    def test_an_aiostreams_that_cant_be_reached_at_the_alias_lookup(self):
        import socket
        from subsync.aiosconfig import AiosApi
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{s.getsockname()[1]}"
        api = lambda base, timeout: AiosApi(closed, timeout=timeout, max_timeout=timeout, retry_wait_cap=5)  # noqa: E731
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.signup(self.s, self.users, dict(self.form("x"), link=f"{INSTANCE}/stremio/u/home/manifest.json"),
                               api_factory=api)
        self.assertEqual((e.exception.message, e.exception.field), (ALIAS_UNREACHABLE, "link"))

    def test_refresh_with_an_alias_link(self):
        uuid, result = self.run_signup(viewer())
        moved = self.fake.add(viewer(), PASSWORD)
        self.fake.aliases["moved"] = moved
        self.signup.refresh(self.s, self.users, result.token,
                            {"password": PASSWORD, "link": f"{INSTANCE}/stremio/u/moved/manifest.json"}, api_factory=self.api)
        self.assertEqual(self.users.open(result.token)["main_uuid"], moved)

    def test_refresh_with_an_unknown_alias_changes_nothing(self):
        uuid, result = self.run_signup(viewer())
        before = self.users.open(result.token)
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.refresh(self.s, self.users, result.token,
                                {"password": PASSWORD, "link": f"{INSTANCE}/stremio/u/gone/manifest.json"},
                                api_factory=self.api)
        self.assertEqual((e.exception.message, e.exception.field), (UNKNOWN_ALIAS, "link"))
        self.assertEqual(self.users.open(result.token), before)

    def test_a_link_with_a_name_and_password_in_it_is_refused(self):
        uuid = "1a2b3c4d-1111-2222-3333-444455556666"
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.parse_link(f"https://me:secret@aio.example.com/stremio/{uuid}/abc/manifest.json")
        self.assertNotIn("secret", e.exception.message)

    def test_an_aiostreams_limiting_new_configs_is_explained(self):
        self.fake.fault = "rate_limited"
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(viewer())
        self.assertIn("hour", e.exception.message)
        self.assertNotIn("this machine", e.exception.message)

    def test_wrong_password_is_plain(self):
        self.fake.fault = "wrong_password"
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(viewer())
        self.assertIn("password", e.exception.message.lower())
        self.assertNotIn(PASSWORD, e.exception.message)

    def test_a_refused_create_is_plain(self):
        self.fake.fault = "refuse_create"
        with self.assertRaises(self.signup.SignupError):
            self.run_signup(viewer())
        self.assertEqual(self.users.count(), 0)

    # -- a config AIOStreams refuses: AIOSubtitles' fault, said plainly -----------------------------------
    def test_a_refused_sync_config_is_said_to_be_aiosubtitles_fault(self):
        self.fake.refusal = lambda config: TESTER_ERROR
        uuid = self.fake.add(viewer(), PASSWORD)
        before = copy.deepcopy(self.fake.configs[uuid]["config"])
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.signup(self.s, self.users, self.form(uuid), api_factory=self.api)
        self.assertEqual(e.exception.message, REFUSED_CONFIG.format(TESTER_ERROR))
        self.assertEqual(self.fake.configs[uuid]["config"], before)      # their own config not changed
        self.assertEqual(len(self.fake.configs), 1)                      # nothing left behind
        self.assertEqual(self.users.count(), 0)

    def test_a_refused_sync_config_at_refresh(self):
        uuid, result = self.run_signup(viewer())
        self.fake.configs[uuid]["config"]["presets"].append(dict(TORRENTIO, instanceId="tio2"))   # a change to follow
        before = copy.deepcopy(self.fake.configs[uuid]["config"])
        self.fake.refusal = lambda config: TESTER_ERROR
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD, "add_addon": "on"},
                                api_factory=self.api)
        self.assertEqual(e.exception.message, REFUSED_CONFIG.format(TESTER_ERROR))
        self.assertEqual(self.fake.configs[uuid]["config"], before)

    def test_the_daily_rebuild_keeps_its_words_for_a_refused_config(self):
        uuid, result = self.run_signup(viewer())
        self.fake.configs[uuid]["config"]["presets"].append(dict(TORRENTIO, instanceId="tio2"))
        self.fake.refusal = lambda config: TESTER_ERROR
        record = self.users.open(result.token)
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.rebuild(self.s, record, result.token, self.api(INSTANCE, 10), PASSWORD)
        self.assertIn("AIOStreams refused the config: " + TESTER_ERROR, e.exception.message)
        self.assertNotIn("fault in AIOSubtitles", e.exception.message)

    def test_a_sync_config_that_does_not_answer_is_removed(self):
        self.fake.fault = "no_manifest"
        with self.assertRaises(self.signup.SignupError):
            uuid, _ = self.run_signup(viewer())
        self.assertEqual(len(self.fake.configs), 1)              # only the person's own config is left
        self.assertEqual(self.users.count(), 0)

    def test_a_failed_save_gives_the_link_and_manual_steps(self):
        self.fake.fault = "refuse_save"
        uuid, result = self.run_signup(viewer())
        self.assertTrue(result.manual)
        self.assertFalse(result.added)
        self.assertIsNotNone(self.users.open(result.token))
        self.assertNotIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))

    def test_unchecked_box_leaves_the_main_config_alone(self):
        uuid, result = self.run_signup(viewer(), add_addon="")
        self.assertFalse(result.added)
        self.assertNotIn(("PUT", "/api/v1/user"), self.fake.requests)
        self.assertEqual(self.fake.configs[uuid]["config"], viewer())

    def test_opensubtitles_is_no_longer_added(self):
        # AIOSubtitles searches OpenSubtitles itself, so a config without subtitle sources gets nothing more
        uuid, result = self.run_signup(viewer(presets=(TORRENTIO,)))
        types = [p["type"] for p in self.fake.configs[uuid]["config"]["presets"]]
        self.assertEqual(types, ["torrentio", "custom"])

    def test_clean_up_takes_out_the_addons_aiosubtitles_searches_itself(self):
        uuid, result = self.run_signup(viewer(), clean_up="on")
        main = self.fake.configs[uuid]["config"]
        self.assertEqual([p["type"] for p in main["presets"]], ["torrentio", "newznab", "custom"])   # no subtitle addon left
        self.assertTrue(any("removes 1 subtitle addon" in c and "OpenSubtitles V3+" in c for c in result.changes))
        record = self.users.open(result.token)
        self.assertEqual([p["type"] for p in self.sync_config(record)["presets"]], ["torrentio"])   # it asks them itself

    def test_clean_up_needs_aiosubtitles_added(self):
        uuid, result = self.run_signup(viewer(), add_addon="", clean_up="on")
        self.assertEqual(self.fake.configs[uuid]["config"], viewer())       # alone it would leave fewer subtitles
        record = self.users.open(result.token)
        self.assertEqual([p["type"] for p in self.sync_config(record)["presets"]], ["torrentio", "opensubtitles-v3-plus"])

    def test_a_hanging_aiostreams_fails_plainly(self):
        self.fake.fault = "hang"
        t0 = time.time()
        with self.assertRaises(self.signup.SignupError):
            self.run_signup(viewer(), timeout=1)
        self.assertLess(time.time() - t0, 2.5)

    def test_unexpected_answers_create_nothing(self):
        for fault in ("html", "bad_config"):
            with self.subTest(fault=fault):
                self.fake.fault = fault
                self.fake.requests.clear()
                with self.assertRaises(self.signup.SignupError):
                    self.run_signup(viewer())
                self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.users.count(), 0)

    def test_languages_up_to_four_default_english(self):
        _, result = self.run_signup(viewer(), languages="")
        self.assertEqual(self.users.open(result.token)["languages"], ["eng"])
        _, result = self.run_signup(viewer(), languages="en, Russian")
        self.assertEqual(self.users.open(result.token)["languages"], ["eng", "rus"])
        self.assertEqual(self.signup.set_languages(self.users, result.token, "German"), ["ger"])
        self.assertEqual(self.users.open(result.token)["languages"], ["ger"])
        with self.assertRaises(self.signup.SignupError):
            self.run_signup(viewer(), languages="en, ru, de, fr, es")
        with self.assertRaises(self.signup.SignupError):
            self.signup.set_languages(self.users, result.token, "Klingon")

    def test_the_answer_for_files_with_no_subtitles_of_their_own(self):
        # Saved with the record when the instance asks it (its audio route on); one it doesn't offer is refused
        # before anything is made; without the field, nothing is saved and the person reads "pick"
        def run(s, **extra):
            uuid = self.fake.add(viewer(), PASSWORD)
            return self.signup.signup(s, self.users, self.form(uuid, **extra), api_factory=self.api, timeout=10)
        env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
               "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_AUDIO_ROUTE": "on"}
        on = Settings(env)
        for answer in ("off", "pick", "open", " Open "):
            with self.subTest(answer=answer):
                result = run(on, audio=answer)
                self.assertEqual(self.users.open(result.token)["audio"], answer.strip().lower())
        self.assertNotIn("audio", self.users.open(run(on).token))
        self.assertNotIn("audio", self.users.open(run(self.s, audio="open").token))     # not asked: route off
        people = self.users.count()
        for answer, s in (("loud", on), ("", on), ("open", Settings(dict(env, SUBSYNC_USER_OPENED_PER_DAY="0")))):
            with self.subTest(refused=answer):
                writes = len(self.fake.writes())
                with self.assertRaises(self.signup.SignupError) as caught:
                    run(s, audio=answer)
                self.assertEqual(caught.exception.field, "audio")
                self.assertEqual(len(self.fake.writes()), writes)
        self.assertEqual(self.users.count(), people)

    def test_signup_with_prowlarr_naming_the_usenet_engine(self):   # refused before the fix
        _, result = self.run_signup(viewer(services=(TORBOX, AIOS_USENET),
                                           presets=(TORRENTIO, PROWLARR, OSV3, NEWZNAB)))
        sync = self.sync_config(self.users.open(result.token))
        self.assertEqual([s["id"] for s in sync["services"]], ["torbox"])
        self.assertEqual([p["options"]["services"] for p in sync["presets"] if p["type"] == "prowlarr"], [["torbox"]])

    # -- refresh and delete ----------------------------------------------------------------------------------------
    def test_refresh_after_adding_prowlarr_naming_the_usenet_engine(self):   # the same at Refresh
        uuid, result = self.run_signup(viewer())
        main = self.fake.configs[uuid]["config"]
        main["services"].append(dict(AIOS_USENET))
        main["presets"].insert(0, copy.deepcopy(PROWLARR))
        self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD}, api_factory=self.api)
        sync = self.sync_config(self.users.open(result.token))
        self.assertEqual([p["options"]["services"] for p in sync["presets"] if p["type"] == "prowlarr"], [["torbox"]])

    def test_refresh_rebuilds_the_sync_config_and_keeps_the_addon(self):
        uuid, result = self.run_signup(viewer())
        main = self.fake.configs[uuid]["config"]
        main["services"].append(dict(REALDEBRID))
        main["presets"].insert(0, {"type": "comet", "instanceId": "cmt", "enabled": True, "options": {"name": "Comet"}})
        again = self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD, "add_addon": "on"},
                                    api_factory=self.api)
        record = self.users.open(result.token)
        sync = self.sync_config(record)
        self.assertEqual([s["id"] for s in sync["services"]], ["torbox"])
        self.assertIn("comet", [p["type"] for p in sync["presets"]])
        self.assertIsNotNone(again.warning)
        names = [p["options"]["name"] for p in self.fake.configs[uuid]["config"]["presets"]]
        self.assertEqual(names.count("AIOSubtitles"), 1)
        self.assertEqual(again.token, result.token)

    def test_refresh_with_a_wrong_password_changes_nothing(self):
        uuid, result = self.run_signup(viewer())
        before = dict(self.fake.configs)
        with self.assertRaises(self.signup.SignupError):
            self.signup.refresh(self.s, self.users, result.token, {"password": "wrong"}, api_factory=self.api)
        self.assertEqual(self.fake.configs, before)

    def test_delete_with_a_wrong_password_deletes_nothing(self):
        uuid, result = self.run_signup(viewer())
        before = {k: dict(v) for k, v in self.fake.configs.items()}
        with self.assertRaises(self.signup.SignupError):
            self.signup.delete(self.s, self.users, result.token, {"password": "wrong"}, api_factory=self.api)
        self.assertIsNotNone(self.users.open(result.token))     # nothing deleted: the person can try again
        self.assertEqual(set(self.fake.configs), set(before))

    def test_the_record_is_written_before_the_main_config_changes(self):
        from unittest import mock
        uuid = self.fake.add(viewer(), PASSWORD)
        with mock.patch.object(self.users, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.signup.signup(self.s, self.users, self.form(uuid), api_factory=self.api)
        self.assertNotIn(("PUT", "/api/v1/user"), self.fake.requests)
        self.assertEqual(len(self.fake.configs), 1)               # the sync config made for it is removed too

    def test_delete_removes_the_sync_config_and_optionally_the_addon(self):
        uuid, result = self.run_signup(viewer())
        sync_uuid = self.users.open(result.token)["sync_uuid"]
        outcome = self.signup.delete(self.s, self.users, result.token, {}, api_factory=self.api)
        self.assertEqual(outcome, {"sync_removed": True, "addon_removed": False})
        self.assertNotIn(sync_uuid, self.fake.configs)
        self.assertIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        self.assertIsNone(self.users.open(result.token))

        uuid, result = self.run_signup(viewer())
        outcome = self.signup.delete(self.s, self.users, result.token, {"password": PASSWORD}, api_factory=self.api)
        self.assertEqual(outcome, {"sync_removed": True, "addon_removed": True})
        self.assertNotIn("AIOSubtitles", str(self.fake.configs[uuid]["config"]))
        self.assertEqual(self.users.count(), 0)

    # -- one AIOStreams, however its link is spelled ------------------------------------------------------
    def test_one_aiostreams_however_its_link_is_spelled(self):
        uuid = "1a2b3c4d-1111-2222-3333-444455556666"
        for host, address in (("AIO.Example.com", "https://aio.example.com"),
                              ("aio.example.com.", "https://aio.example.com"),
                              ("aio.example.com:443", "https://aio.example.com"),
                              ("AIO.example.com:8443", "https://aio.example.com:8443"),
                              ("[2001:DB8::1]:443", "https://[2001:db8::1]"), ("[::1]:8443", "https://[::1]:8443"),
                              ("aio.example.com:abc", "https://aio.example.com:abc")):   # not a port: kept as typed
            with self.subTest(host=host):
                self.assertEqual(self.signup.parse_link(f"https://{host}/Aio/stremio/{uuid}/abc/manifest.json"),
                                 (f"{address}/Aio", uuid, None))
                self.assertEqual(self.signup.instance_key(f"https://{host}/Aio"), f"{address}/Aio")

    def test_a_refresh_on_the_same_aiostreams_spelled_otherwise_is_not_a_move(self):
        import urllib.parse
        from subsync.aiosconfig import AiosApi

        def api(base, timeout):      # every spelling of the AIOStreams reaches the stand-in
            return AiosApi(self.fake.url + urllib.parse.urlsplit(base).path, timeout=timeout, max_timeout=timeout,
                           retry_wait_cap=5)
        uuid, result = self.run_signup(viewer())
        before = self.users.open(result.token)
        self.fake.requests.clear()
        spelled = f"https://AIOS.test:443/stremio/{uuid}/ENCRYPTED/manifest.json"
        self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD, "link": spelled}, api_factory=api)
        after = self.users.open(result.token)
        self.assertEqual((after["instance"], after["sync_uuid"]), (before["instance"], before["sync_uuid"]))
        self.assertNotIn(("POST", "/api/v1/user"), self.fake.requests)      # nothing made again, nothing removed
        self.assertNotIn(("DELETE", "/api/v1/user"), self.fake.requests)
        # a record from before kept the address as it was typed: still the same AIOStreams, and kept as it is
        after["instance"] = "https://AIOS.test:443"
        self.users.update(result.token, after)
        self.signup.refresh(self.s, self.users, result.token,
                            {"password": PASSWORD, "link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json"},
                            api_factory=api)
        again = self.users.open(result.token)
        self.assertEqual((again["instance"], again["sync_uuid"]), ("https://AIOS.test:443", before["sync_uuid"]))
        self.assertNotIn(("POST", "/api/v1/user"), self.fake.requests)

    # -- nothing left behind, whatever fails ---------------------------------------------
    def faulty(self, method):
        """The stand-in's client with `method` raising an error the client never gives: a fault of AIOSubtitles' own."""
        from unittest import mock

        def factory(base, timeout):
            api = self.api(base, timeout)
            setattr(api, method, mock.Mock(side_effect=RuntimeError("a fault")))
            return api
        return factory

    def test_a_fault_while_checking_a_new_config_removes_it(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        with self.assertRaises(RuntimeError):
            self.signup.signup(self.s, self.users, self.form(uuid), api_factory=self.faulty("addon_get"))
        self.assertEqual(list(self.fake.configs), [uuid])        # the sync config made for it is removed again
        self.assertEqual(self.users.count(), 0)

    def test_a_fault_in_a_clean_up_never_hides_the_first_error(self):
        from unittest import mock
        uuid = self.fake.add(viewer(), PASSWORD)
        with mock.patch.object(self.users, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.signup.signup(self.s, self.users, self.form(uuid), api_factory=self.faulty("delete_config"))
        self.assertIn("removing a config failed: RuntimeError", self.logs.lines)   # by its kind only

    def test_a_fault_after_the_record_was_saved_still_gives_the_link(self):
        from unittest import mock
        with mock.patch.object(self.signup, "viewer_change", side_effect=RuntimeError("a fault")):
            uuid, result = self.run_signup(viewer())
        self.assertEqual((result.added, result.manual, result.changes), (False, True, []))   # added by hand
        self.assertIsNotNone(self.users.open(result.token))
        self.assertNotIn(("PUT", "/api/v1/user"), self.fake.requests)

    def test_an_aiostreams_listing_preset_options_of_another_shape(self):
        options = [None, "timeout", {"default": 1}, {"id": ["x"], "default": 1}, {"id": "maxSubs", "default": 9}]
        for shape in (None, "x", {"id": "timeout"}, options):
            with self.subTest(shape=shape):
                self.fake.STATUS = {"success": True, "data": {"version": "2.37.0", "settings": {"presets": [
                    {"ID": "custom", "OPTIONS": shape}]}}}
                uuid, result = self.run_signup(viewer())
                self.assertTrue(result.added)
                added = [p for p in self.fake.configs[uuid]["config"]["presets"] if p["type"] == "custom"]
                self.assertEqual(added[0]["options"].get("maxSubs"), 9 if shape is options else None)

    def test_a_fault_removing_old_configs_after_a_refresh(self):
        uuid, result = self.run_signup(viewer())
        main = self.fake.configs[uuid]["config"]
        main["services"] = [dict(REALDEBRID)]          # stored copies only now: the sync config is removed
        again = self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD},
                                    api_factory=self.faulty("delete_config"))
        self.assertTrue(again.store_only)
        self.assertTrue(self.users.open(result.token)["store_only"])
        self.assertIn("removing a config failed: RuntimeError", self.logs.lines)

    def test_a_fault_while_deleting_still_deletes_the_record(self):
        for method, form, outcome in (("delete_config", {}, {"sync_removed": False, "addon_removed": False}),
                                      ("save_config", {"password": PASSWORD},
                                       {"sync_removed": True, "addon_removed": False})):
            with self.subTest(method=method):
                uuid, result = self.run_signup(viewer())
                self.assertEqual(self.signup.delete(self.s, self.users, result.token, form,
                                                    api_factory=self.faulty(method)), outcome)
                self.assertIsNone(self.users.open(result.token))

    def test_delete_waits_for_the_record_lock(self):
        import threading
        uuid, result = self.run_signup(viewer())
        done = []

        def delete():
            done.append(self.signup.delete(self.s, self.users, result.token, {}, api_factory=self.api, timeout=10))
        with self.users.locked(result.token):          # a refresh or the daily check working on the record
            thread = threading.Thread(target=delete, daemon=True)
            thread.start()
            thread.join(0.5)
            self.assertEqual(done, [])
            self.assertIsNotNone(self.users.open(result.token))
        thread.join(10)
        self.assertEqual(done, [{"sync_removed": True, "addon_removed": False}])
        self.assertIsNone(self.users.open(result.token))

    def test_a_refresh_whose_record_went_meanwhile_leaves_nothing_behind(self):
        uuid, result = self.run_signup(viewer())
        del self.fake.configs[self.users.open(result.token)["sync_uuid"]]   # gone: the refresh makes it again

        def api(base, timeout):
            client = self.api(base, timeout)
            create = client.create_config

            def made_then_expired(config, password):
                made = create(config, password)
                self.users.delete(result.token)        # the record expires meanwhile (no lock keeps that out)
                return made
            client.create_config = made_then_expired
            return client
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.refresh(self.s, self.users, result.token, {"password": PASSWORD}, api_factory=api)
        self.assertEqual(e.exception.message, "This link isn't signed up (any more). Sign up again on the start page.")
        self.assertEqual(list(self.fake.configs), [uuid])        # the config made by the refresh is removed again

    def test_a_deletion_is_logged_only_when_something_was_kept(self):
        _, result = self.run_signup(viewer(), keep_password="on")
        self.signup.forget_password(self.users, result.token)
        self.signup.forget_password(self.users, result.token)          # pressed again: nothing left to delete
        self.assertEqual(self.logs.lines.count("a kept password was deleted"), 1)
        self.signup.remove_tmdb_key(self.users, result.token)          # no key kept
        self.assertNotIn("a person's TMDB key was deleted", self.logs.lines)
        self.signup.set_tmdb_key(self.users, result.token, "a" * 32, self.s, check=lambda s, key: True)
        self.signup.remove_tmdb_key(self.users, result.token)
        self.assertEqual(self.logs.lines.count("a person's TMDB key was deleted"), 1)


class UsenetServicesInAddonsTest(unittest.TestCase):
    """An addon's own Services choice can name a Usenet service that the sync config leaves out, and AIOStreams
    refuses a config whose addon names a service it doesn't have. No sign-up store needed, so these run
    without the cryptography package too."""

    def setUp(self):
        from subsync.aiosconfig import AiosApi
        self.fake = FakeAiostreams()
        self.api = AiosApi(self.fake.url, timeout=10, max_timeout=10, retry_wait_cap=5)

    def tearDown(self):
        self.fake.close()

    def test_the_stand_in_refuses_what_aiostreams_refused_for_the_tester(self):
        from subsync.aiosconfig import AiosError
        with self.assertRaises(AiosError) as e:
            self.api.create_config(viewer(services=(TORBOX,), presets=(TORRENTIO, PROWLARR)), "pw")
        self.assertEqual(e.exception.message, "Creating the sync config: AIOStreams refused the config: " + TESTER_ERROR)

    def test_the_sync_config_of_a_config_with_prowlarr_naming_the_usenet_engine_is_accepted(self):
        from subsync.aiosconfig import sync_config
        sync = sync_config(viewer(services=(TORBOX, AIOS_USENET), presets=(TORRENTIO, PROWLARR, OSV3, NEWZNAB)))
        uuid, _ = self.api.create_config(sync, "pw")
        made = self.fake.configs[uuid]["config"]
        self.assertEqual([s["id"] for s in made["services"]], ["torbox"])
        self.assertEqual([p["options"]["services"] for p in made["presets"] if p["type"] == "prowlarr"], [["torbox"]])


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.now = [1_800_000_000.0]
        self.count = [0]

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def gate(self, **env):
        from subsync.signup import SignupGate
        s = Settings(dict({"SUBSYNC_PUBLIC": "on"}, **env))
        return SignupGate(s, lambda: self.count[0], clock=lambda: self.now[0])

    def test_invite(self):
        from subsync.signup import SignupError
        gate = self.gate(SUBSYNC_PUBLIC_INVITE="letmein")
        with self.assertRaises(SignupError):
            gate.check("1.2.3.4", "wrong")
        with self.assertRaises(SignupError):
            gate.check("1.2.3.4", "")
        gate.check("1.2.3.4", "letmein")
        self.gate().check("1.2.3.4", "")             # no code set: anyone

    def test_wrong_invite_codes_count_as_attempts(self):
        from subsync.signup import SignupError
        gate = self.gate(SUBSYNC_PUBLIC_INVITE="letmein")
        for _ in range(5):
            with self.assertRaises(SignupError):
                gate.check("1.2.3.4", "guess")
        with self.assertRaises(SignupError) as e:
            gate.check("1.2.3.4", "letmein")            # even the right code: too many tries this hour
        self.assertEqual(e.exception.code, 429)

    def test_attempts_per_address_per_hour(self):
        from subsync.signup import SignupError
        gate = self.gate()
        for _ in range(5):
            gate.check("1.2.3.4", "")
        with self.assertRaises(SignupError):
            gate.check("1.2.3.4", "")               # the 6th in an hour
        gate.check("5.6.7.8", "")                    # another address
        self.now[0] += 3601
        gate.check("1.2.3.4", "")                    # an hour later

    def test_full(self):
        from subsync.signup import SignupError
        gate = self.gate(SUBSYNC_MAX_USERS="2")
        self.count[0] = 2
        with self.assertRaises(SignupError) as e:
            gate.check("1.2.3.4", "")
        self.assertIn("full", e.exception.message)


if __name__ == "__main__":
    unittest.main()
