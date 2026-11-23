"""Public mode's sign-up, refresh and delete, against a stand-in AIOStreams on this machine. The password is used once
and never stored or logged; Real-Debrid is warned about and left out, or, when it is the only service, the sign-up is
for stored copies only."""
import copy
import os
import time
import unittest

from helpers import (FakeAiostreams, files_bytes, HAVE_CRYPTOGRAPHY, INSTANCE, KEY, NEEDS, PASSWORD,
                     SignupHarness, TempDir)
from subsync.settings import Settings

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


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class SignupTest(SignupHarness, unittest.TestCase):
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
        data = files_bytes(self.dir, "users.sqlite3")
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

    def test_a_config_with_no_debrid_service_is_told_in_aiosubtitles_name(self):
        # Public pages never say "subsync"
        with self.assertRaises(self.signup.SignupError) as e:
            self.run_signup(viewer(services=()))
        self.assertEqual(e.exception.message, "AIOSubtitles can't find any file it could read through your AIOStreams: "
                                              "no debrid service (AIOSubtitles reads files from debrid services only, "
                                              "not Usenet or torrents).")

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
        # The page's own step only, AIOStreams' reason quoted as its words (the daily check's notice detail)
        self.assertEqual(e.exception.message, "Updating AIOSubtitles' config on your AIOStreams: your AIOStreams "
                                              f"refused the config: \"{TESTER_ERROR}\".")
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

    def test_aiostreams_refusing_aiosubtitles_in_their_config_is_logged_for_the_operator(self):
        # An AIOStreams answer at this step left no log line, so an AIOStreams that refused every AIOSubtitles
        # entry sent each person to the manual steps unseen. Logged by its kind or code, never by AIOStreams' words,
        # which may quote the personal link
        from unittest import mock
        from subsync.aiosconfig import AiosError

        def entry_link(config):
            return next((p["options"]["manifestUrl"] for p in config.get("presets") or []
                         if "/u/" in (p.get("options") or {}).get("manifestUrl", "")), None)
        self.fake.refusal = lambda config: entry_link(config) and f"Invalid addon {entry_link(config)}"
        uuid, result = self.run_signup(viewer())
        self.assertEqual((result.added, result.manual), (False, True))
        self.assertIn("adding AIOSubtitles to a config failed: CONFIG_REFUSED", self.logs.lines)
        self.fake.refusal = lambda config: None

        def answering(**error):
            def factory(base, timeout):
                api = self.api(base, timeout)
                api.save_config = mock.Mock(side_effect=AiosError("Saving the config: words", **error))
                return api
            return factory
        for error, logged in (({"code": "INTERNAL"}, "INTERNAL"), ({"code": 502}, "502"),
                              ({"code": "https://subs.test/u/x"}, "an error answer")):
            with self.subTest(code=error["code"]):
                uuid = self.fake.add(viewer(), PASSWORD)
                self.logs.lines.clear()
                result = self.signup.signup(self.s, self.users, self.form(uuid), api_factory=answering(**error))
                self.assertTrue(result.manual)
                self.assertIn(f"adding AIOSubtitles to a config failed: {logged}", self.logs.lines)

        def unsaved(base, timeout):        # AIOStreams says it saved, and the read-back has no AIOSubtitles
            api = self.api(base, timeout)
            api.save_config = mock.Mock(return_value=None)
            return api
        uuid = self.fake.add(viewer(), PASSWORD)
        result = self.signup.signup(self.s, self.users, self.form(uuid), api_factory=unsaved)
        self.assertTrue(result.manual)
        self.assertIn("adding AIOSubtitles to a config failed: saved, but not found when read back", self.logs.lines)
        for line in self.logs.lines:
            self.assertNotIn("/u/", line)
            self.assertNotIn(PASSWORD, line)

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

    def test_clean_up_takes_out_only_the_addons_whose_site_the_instance_asks(self):
        # SubDL and SubSource are asked only with the service's key; without it a person's own SubDL and
        # SubSource addons stay, in their config and in AIOSubtitles' own
        from subsync.settings import Settings
        subdl = {"type": "subdl", "instanceId": "sdl", "enabled": True,
                 "options": {"name": "SubDL", "resources": ["subtitles"]}}
        subsource = {"type": "subsource", "instanceId": "ssr", "enabled": True,
                     "options": {"name": "SubSource", "resources": ["subtitles"]}}
        config = viewer(presets=(TORRENTIO, OSV3, subdl, subsource))
        uuid, result = self.run_signup(config, clean_up="on")
        main = [p["type"] for p in self.fake.configs[uuid]["config"]["presets"]]
        self.assertEqual(main, ["torrentio", "custom", "subdl", "subsource"])
        self.assertTrue(any(c.startswith("removes 1 subtitle addon") for c in result.changes), result.changes)
        sync = self.sync_config(self.users.open(result.token))
        self.assertEqual([p["type"] for p in sync["presets"]], ["torrentio", "subdl", "subsource"])
        self.s = Settings(dict(self.env, SUBSYNC_SUBDL_KEY="k1", SUBSYNC_SUBSOURCE_KEY="k2"))   # both asked: all go
        uuid, result = self.run_signup(config, clean_up="on")
        self.assertEqual([p["type"] for p in self.fake.configs[uuid]["config"]["presets"]], ["torrentio", "custom"])
        self.assertTrue(any(c.startswith("removes 3 subtitle addons") for c in result.changes), result.changes)

    def test_clean_up_needs_aiosubtitles_added(self):
        uuid, result = self.run_signup(viewer(), add_addon="", clean_up="on")
        self.assertEqual(self.fake.configs[uuid]["config"], viewer())       # alone it would leave fewer subtitles
        record = self.users.open(result.token)
        self.assertEqual([p["type"] for p in self.sync_config(record)["presets"]], ["torrentio", "opensubtitles-v3-plus"])

    def test_a_hanging_aiostreams_fails_plainly(self):
        self.fake.fault = "hang"
        t0 = time.monotonic()
        with self.assertRaises(self.signup.SignupError):
            self.run_signup(viewer(), timeout=1)
        self.assertLess(time.monotonic() - t0, 2.5)

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
        before = copy.deepcopy(self.fake.configs)       # a shallow copy held the very configs a save changes
        with self.assertRaises(self.signup.SignupError):
            self.signup.refresh(self.s, self.users, result.token, {"password": "wrong"}, api_factory=self.api)
        self.assertEqual(self.fake.configs, before)

    def test_delete_with_a_wrong_password_deletes_nothing(self):
        uuid, result = self.run_signup(viewer())
        # the configs whole, not only their ids, and no write at all (the twin)
        before, writes = copy.deepcopy(self.fake.configs), len(self.fake.writes())
        with self.assertRaises(self.signup.SignupError):
            self.signup.delete(self.s, self.users, result.token, {"password": "wrong"}, api_factory=self.api)
        self.assertIsNotNone(self.users.open(result.token))     # nothing deleted: the person can try again
        self.assertEqual(self.fake.configs, before)
        self.assertEqual(len(self.fake.writes()), writes)

    def test_delete_says_when_it_is_certain_before_it_removes_anything(self):
        # The router stopped the person's service, forgetting them, before a mistyped password deleted nothing
        uuid, result = self.run_signup(viewer())
        sync_uuid = self.users.open(result.token)["sync_uuid"]
        seen = []

        def confirmed():
            seen.append((sync_uuid in self.fake.configs, self.users.open(result.token) is not None))
        with self.assertRaises(self.signup.SignupError):
            self.signup.delete(self.s, self.users, result.token, {"password": "wrong"}, api_factory=self.api,
                               on_confirmed=confirmed)
        self.assertEqual(seen, [])                                # a mistyped password: not certain
        for form in ({"password": PASSWORD}, {}):                # the password accepted, or none typed
            with self.subTest(password=bool(form)):
                if not form:
                    uuid, result = self.run_signup(viewer())
                    sync_uuid = self.users.open(result.token)["sync_uuid"]
                seen.clear()
                self.signup.delete(self.s, self.users, result.token, form, api_factory=self.api,
                                   on_confirmed=confirmed)
                self.assertEqual(seen, [(True, True)])            # once, before anything is removed
                self.assertIsNone(self.users.open(result.token))

    def test_the_record_is_written_before_the_main_config_changes(self):
        from unittest import mock
        uuid = self.fake.add(viewer(), PASSWORD)
        with mock.patch.object(self.users, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.signup.signup(self.s, self.users, self.form(uuid), api_factory=self.api)
        self.assertNotIn(("PUT", "/api/v1/user"), self.fake.requests)
        self.assertEqual(len(self.fake.configs), 1)               # the sync config made for it is removed too

    def test_the_user_cap_is_counted_again_when_the_record_is_written(self):
        # The gate counts the records before the sign-up's AIOStreams calls, and nothing counted them again,
        # so sign-ups running at once all passed it and the instance ended past SUBSYNC_MAX_USERS
        from subsync.users import new_token
        uuid = self.fake.add(viewer(), PASSWORD)
        s = Settings(dict(self.env, SUBSYNC_MAX_USERS="1"))
        self.signup.SignupGate(s, self.users.count).check("1.2.3.4", "")    # one place left: the gate lets it in

        def api(base, timeout):
            client = self.api(base, timeout)
            create = client.create_config

            def made_while_another_signed_up(config, password):
                self.users.create(new_token(), {"x": 1})       # another sign-up takes the last place meanwhile
                return create(config, password)
            client.create_config = made_while_another_signed_up
            return client
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.signup(s, self.users, self.form(uuid), api_factory=api)
        self.assertEqual((e.exception.code, e.exception.message),
                         (503, "AIOSubtitles is full for now. Try again in a few days."))
        self.assertEqual(self.users.count(), 1)                   # only the other person's record
        self.assertEqual(list(self.fake.configs), [uuid])         # the sync config made for it is removed again
        self.assertNotIn(("PUT", "/api/v1/user"), self.fake.requests)   # their own config not changed

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
                              ("[2001:DB8::1]:443", "https://[2001:db8::1]"), ("[::1]:8443", "https://[::1]:8443")):
            with self.subTest(host=host):
                self.assertEqual(self.signup.parse_link(f"https://{host}/Aio/stremio/{uuid}/abc/manifest.json"),
                                 (f"{address}/Aio", uuid, None))
                self.assertEqual(self.signup.instance_key(f"https://{host}/Aio"), f"{address}/Aio")
        # not a port: a link with it is refused; an address with it is kept as typed
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.parse_link(f"https://aio.example.com:abc/Aio/stremio/{uuid}/abc/manifest.json")
        self.assertEqual((e.exception.message, e.exception.field), (self.signup.NOT_A_LINK, "link"))
        self.assertEqual(self.signup.instance_key("https://aio.example.com:abc/Aio"), "https://aio.example.com:abc/Aio")

    # -- a link that can't be read ----------------------------------------------------------------
    def test_a_link_that_cant_be_read_is_the_link_error_before_anything_is_asked(self):
        # a port that isn't a number, or out of range, or an unmatched "[": a blank page at sign-up and Refresh, or
        # "can't reach your AIOStreams", instead of the link error
        uuid, result = self.run_signup(viewer())
        self.fake.requests.clear()
        for link in (f"https://aios.test:abc/stremio/{uuid}/ENCRYPTED/manifest.json",
                     f"https://aios.test:99999/stremio/{uuid}/ENCRYPTED/manifest.json",
                     f"https://[aios.test/stremio/{uuid}/ENCRYPTED/manifest.json",
                     "https://[aios.test/stremio/u/Family.TV/manifest.json"):
            for step, call in (
                    ("sign-up", lambda: self.signup.signup(self.s, self.users, dict(self.form(uuid), link=link),
                                                           api_factory=self.api, timeout=10)),
                    ("refresh", lambda: self.signup.refresh(self.s, self.users, result.token,
                                                            {"password": PASSWORD, "link": link},
                                                            api_factory=self.api, timeout=10))):
                with self.subTest(link=link, step=step):
                    with self.assertRaises(self.signup.SignupError) as e:
                        call()
                    self.assertEqual((e.exception.message, e.exception.field), (self.signup.NOT_A_LINK, "link"))
        self.assertEqual(self.fake.requests, [])

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

    def test_a_config_already_gone_counts_as_removed_at_delete(self):
        # Delete said a config AIOStreams no longer has stays there with the person's debrid keys; one that may
        # really be left (an error of AIOStreams') still says so
        from subsync.aiosconfig import AiosError
        from unittest import mock
        uuid, result = self.run_signup(viewer())
        del self.fake.configs[self.users.open(result.token)["sync_uuid"]]      # removed in AIOStreams meanwhile
        self.assertEqual(self.signup.delete(self.s, self.users, result.token, {}, api_factory=self.api),
                         {"sync_removed": True, "addon_removed": False})
        self.assertIsNone(self.users.open(result.token))

        def failing(base, timeout):
            api = self.api(base, timeout)
            api.delete_config = mock.Mock(side_effect=AiosError("Internal error", code="INTERNAL"))
            return api
        uuid, result = self.run_signup(viewer())
        self.assertEqual(self.signup.delete(self.s, self.users, result.token, {}, api_factory=failing),
                         {"sync_removed": False, "addon_removed": False})
        self.assertIsNone(self.users.open(result.token))

    def test_unticking_keep_my_password_deletes_it_even_when_the_refresh_fails(self):
        # The kept password went only once the whole refresh had worked, so after a mistyped password or an
        # AIOStreams that was down it stayed, and the daily rebuild went on using it
        from subsync.aiosconfig import AiosApi

        def down(base, timeout):
            return AiosApi("https://127.0.0.1:9", timeout=timeout, max_timeout=timeout, retry_wait_cap=5)
        for why, form, api in (("a mistyped password", {"password": "not-the-password"}, self.api),
                               ("AIOStreams down", {"password": PASSWORD}, down)):
            with self.subTest(why=why):
                uuid, result = self.run_signup(viewer(), keep_password="on")
                self.assertEqual(self.users.open(result.token)["auto_password"], PASSWORD)
                self.logs.lines.clear()
                with self.assertRaises(self.signup.SignupError):
                    self.signup.refresh(self.s, self.users, result.token, form, api_factory=api, timeout=5)
                self.assertNotIn("auto_password", self.users.open(result.token))
                self.assertEqual(self.logs.lines.count("a kept password was deleted"), 1)
        uuid, result = self.run_signup(viewer(), keep_password="on")       # ticked: kept as before, the refresh failing
        with self.assertRaises(self.signup.SignupError):
            self.signup.refresh(self.s, self.users, result.token, {"password": "x", "keep_password": "on"},
                                api_factory=self.api)
        self.assertEqual(self.users.open(result.token)["auto_password"], PASSWORD)

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

    def test_a_refresh_that_fails_saving_the_record_leaves_their_own_config_alone(self):
        # Refresh put AIOSubtitles back into the person's own config before it saved the record, so a record
        # that went meanwhile, or a write that failed, left their config changed though the refresh had failed
        from unittest import mock

        def without_entry(uuid):
            main = self.fake.configs[uuid]["config"]
            main["presets"] = [p for p in main["presets"] if p["options"]["name"] != "AIOSubtitles"]  # they took it out
            return copy.deepcopy(main)

        def expiring(token):
            def factory(base, timeout):
                client = self.api(base, timeout)
                create = client.create_config

                def made_then_expired(config, password):
                    made = create(config, password)
                    self.users.delete(token)               # the record expires meanwhile (no lock keeps that out)
                    return made
                client.create_config = made_then_expired
                return client
            return factory
        form = {"password": PASSWORD, "add_addon": "on"}
        uuid, result = self.run_signup(viewer())
        before = without_entry(uuid)
        del self.fake.configs[self.users.open(result.token)["sync_uuid"]]   # gone: the refresh makes it again
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.refresh(self.s, self.users, result.token, form, api_factory=expiring(result.token))
        self.assertEqual(e.exception.message, "This link isn't signed up (any more). Sign up again on the start page.")
        self.assertEqual(self.fake.configs[uuid]["config"], before)    # their own config not changed

        uuid, result = self.run_signup(viewer())
        before = without_entry(uuid)
        with mock.patch.object(self.users, "update", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.signup.refresh(self.s, self.users, result.token, form, api_factory=self.api)
        self.assertEqual(self.fake.configs[uuid]["config"], before)
        again = self.signup.refresh(self.s, self.users, result.token, form, api_factory=self.api)   # once it saves
        self.assertTrue(again.added)
        names = [p["options"]["name"] for p in self.fake.configs[uuid]["config"]["presets"]]
        self.assertEqual(names.count("AIOSubtitles"), 1)

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


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class KeptPasswordTest(SignupHarness, unittest.TestCase):
    """Only the check uses a kept password, so with it off (SUBSYNC_RECHECK_HOURS 0) none is kept, even from a
    form that sends the box ticked, and one kept before goes at the next refresh."""

    public_env = {"SUBSYNC_RECHECK_HOURS": "0"}

    def refresh(self, settings, token):
        self.signup.refresh(settings, self.users, token, {"password": PASSWORD, "keep_password": "on"},
                            api_factory=self.api, timeout=10)
        return self.users.open(token).get("auto_password")

    def test_no_password_is_kept_while_the_check_is_off(self):
        uuid = self.fake.add(viewer(), PASSWORD)
        form = {"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                "languages": "English", "add_addon": "on", "keep_password": "on"}
        token = self.signup.signup(self.s, self.users, form, api_factory=self.api, timeout=10).token
        self.assertNotIn("auto_password", self.users.open(token))
        record = self.users.open(token)
        record["auto_password"] = PASSWORD                  # kept before the check was turned off
        self.users.update(token, record)
        self.assertIsNone(self.refresh(self.s, token))
        on = Settings(dict(self.env, SUBSYNC_RECHECK_HOURS="24"))
        self.assertEqual(self.refresh(on, token), PASSWORD)  # with the check on, the same box keeps it


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


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
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

    def test_capitals_dont_matter_in_an_invite_code(self):
        # A phone typed setup's suggested code in capitals, and it was refused, every try counting
        from subsync import explain
        from subsync.signup import SignupError
        gate = self.gate(SUBSYNC_PUBLIC_INVITE="k7mq-3xtd-p9wa", SUBSYNC_SIGNUPS_PER_HOUR="10")
        for typed in ("K7MQ-3XTD-P9WA", " k7mq-3XTD-p9wa ", "k7mq-3xtd-p9wa"):
            gate.check("1.2.3.4", typed)
        gate = self.gate(SUBSYNC_PUBLIC_INVITE="LetMeIn")
        gate.check("1.2.3.4", "letmein")
        with self.assertRaises(SignupError):
            gate.check("1.2.3.4", "letmeout")
        self.assertEqual(explain.SETTINGS["SUBSYNC_PUBLIC_INVITE"][1],
                         "When set, the sign-up page asks for this code, so only people you gave it to can sign up, for "
                         "example while you and your testers try the service. Capitals don't matter. Empty lets anyone "
                         "sign up. It doesn't affect people who already signed up. Recommended while testing; remove it "
                         "to open the service.")

    def test_wrong_invite_codes_count_as_attempts(self):
        from subsync.signup import SignupError
        gate = self.gate(SUBSYNC_PUBLIC_INVITE="letmein", SUBSYNC_SIGNUPS_PER_HOUR="3")   # the setting read
        for _ in range(gate.s.signups_per_hour):
            with self.assertRaises(SignupError):
                gate.check("1.2.3.4", "guess")
        with self.assertRaises(SignupError) as e:
            gate.check("1.2.3.4", "letmein")            # even the right code: too many tries this hour
        self.assertEqual(e.exception.code, 429)

    def test_attempts_per_address_per_hour(self):
        from subsync.signup import SignupError
        gate = self.gate(SUBSYNC_SIGNUPS_PER_HOUR="3")   # the setting read, not its default
        for _ in range(gate.s.signups_per_hour):
            gate.check("1.2.3.4", "")
        with self.assertRaises(SignupError):
            gate.check("1.2.3.4", "")               # one more in an hour
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
