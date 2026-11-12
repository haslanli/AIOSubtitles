"""Public mode's sign-up, refresh and delete, against a stand-in AIOStreams on this machine. The password is used once
and never stored or logged; Real-Debrid is warned about and left out, or stops the sign-up when it is the only service."""
import logging
import os
import sys
import time
import unittest

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
TORRENTIO = {"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}}
OSV3 = {"type": "opensubtitles-v3-plus", "instanceId": "osp", "enabled": True,
        "options": {"name": "OpenSubtitles V3+", "resources": ["subtitles"]}}
NEWZNAB = {"type": "newznab", "instanceId": "nzb", "enabled": True, "options": {"name": "Indexer"}}
TORBOX = {"id": "torbox", "enabled": True, "credentials": {"apiKey": "tb-key"}}
REALDEBRID = {"id": "realdebrid", "enabled": True, "credentials": {"apiKey": "rd-key"}}


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

    def test_real_debrid_only_stops_and_creates_nothing(self):
        from subsync import realdebrid
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(viewer(services=(REALDEBRID,)))
        self.assertEqual(e.exception.message, realdebrid.REAL_DEBRID_ONLY)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.users.count(), 0)

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
                         ("https://aio.example.com", uuid))
        self.assertEqual(self.signup.parse_link(f"https://example.com/aio/stremio/{uuid.upper()}/abc/manifest.json"),
                         ("https://example.com/aio", uuid))

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

    # -- refresh and delete ----------------------------------------------------------------------------------------
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
