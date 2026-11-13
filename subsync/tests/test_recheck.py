"""Public mode: each person's AIOStreams checked again about once a day while they use the service, against a stand-in
AIOStreams on this machine. Without a kept password the check sees only what needs no password (does their own config
still exist; AIOSubtitles' own config, with its own password) and repairs what it can; with the password a person chose
to let AIOSubtitles keep, it also rebuilds AIOSubtitles' config from theirs. It never changes the person's own config."""
import json
import logging
import os
import sys
import threading
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
OTHER = "https://other-aios.test"
PASSWORD = "the-viewers-own-Password-1"
TORBOX = {"id": "torbox", "enabled": True, "credentials": {"apiKey": "tb-key"}}
REALDEBRID = {"id": "realdebrid", "enabled": True, "credentials": {"apiKey": "rd-key"}}
TORRENTIO = {"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}}
COMET = {"type": "comet", "instanceId": "cmt", "enabled": True, "options": {"name": "Comet"}}
HOUR = 3600


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class Base(unittest.TestCase):
    def setUp(self):
        from subsync import signup
        from subsync.aiosconfig import AiosApi
        from subsync.users import Users
        self.signup = signup
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.clock = Clock()
        self.s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                           "SUBSYNC_PUBLIC_BASE": "https://subs.test"})
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes(), clock=self.clock)
        self.fake = FakeAiostreams()
        self.dead = False      # True: every AIOStreams answers nothing (a closed port)

        def api(base, timeout):
            url = "http://127.0.0.1:9" if self.dead else base.replace(INSTANCE, self.fake.url).replace(OTHER, self.fake.url)
            return AiosApi(url, timeout=timeout, max_timeout=timeout, retry_wait_cap=5)
        self.api = api
        self.changed = []
        self.logs = Logs()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.logs)
        self.fake.close()
        self.users.close()
        self.tmp.__exit__(None, None, None)

    def checker(self, **env):
        from subsync.recheck import Checker
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                           "SUBSYNC_PUBLIC_BASE": "https://subs.test"}, **env))
        return Checker(s, self.users, on_change=self.changed.append, api_factory=self.api, clock=self.clock, timeout=5)

    def sign_up(self, keep=False, presets=(TORRENTIO,)):
        viewer = {"services": [dict(TORBOX)], "presets": [dict(p) for p in presets]}
        uuid = self.fake.add(viewer, PASSWORD)
        form = {"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                "languages": "English", "add_addon": "on"}
        if keep:
            form["keep_password"] = "on"
        result = self.signup.signup(self.s, self.users, form, api_factory=self.api, timeout=5)
        return uuid, result.token

    def record(self, token):
        return self.users.open(token)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class RecheckTest(Base):
    # -- the check without a kept password ------------------------------------------------------------------------
    def test_a_new_record_says_when_its_config_was_copied(self):
        uuid, token = self.sign_up()
        record = self.record(token)
        self.assertEqual(record["synced_at"], self.clock.t)
        self.assertNotIn("auto_password", record)
        self.assertIsNone(record.get("state"))

    def test_nothing_changed(self):
        uuid, token = self.sign_up()
        sync_uuid = self.record(token)["sync_uuid"]
        before = len(self.fake.requests)
        self.assertIsNone(self.checker().check(token))
        asked = self.fake.requests[before:]
        self.assertEqual(sorted(m for m, _ in asked), ["GET", "HEAD"])       # two calls, both reads
        self.assertEqual(self.fake.auth_ids[-1], sync_uuid)                   # only AIOSubtitles' own config is opened
        self.assertNotIn(uuid, self.fake.auth_ids[-1:])
        record = self.record(token)
        self.assertEqual(record["checked_at"], self.clock.t)
        self.assertEqual(record["next_check"], self.clock.t + 24 * HOUR)
        self.assertEqual(self.changed, [])

    def test_a_changed_sync_link_is_repaired(self):
        uuid, token = self.sign_up()
        sync_uuid = self.record(token)["sync_uuid"]
        self.fake.encrypted[sync_uuid] = "NEWENCRYPTED"
        self.assertIsNone(self.checker().check(token))
        self.assertEqual(self.record(token)["sync_link"], f"{INSTANCE}/stremio/{sync_uuid}/NEWENCRYPTED/manifest.json")
        self.assertEqual(len(self.changed), 1)        # the person's worker starts again with the new link

    def test_a_gone_sync_config_needs_a_refresh(self):
        uuid, token = self.sign_up()
        del self.fake.configs[self.record(token)["sync_uuid"]]
        self.assertEqual(self.checker().check(token), "sync_gone")
        self.assertEqual(self.record(token)["state"], "sync_gone")
        self.assertEqual(len(self.changed), 1)

    def test_their_own_config_gone_counts_after_a_second_look_an_hour_later(self):
        uuid, token = self.sign_up()
        del self.fake.configs[uuid]
        checker = self.checker()
        self.assertIsNone(checker.check(token))                          # once could be a slip
        self.assertEqual(self.record(token)["next_check"], self.clock.t + HOUR)
        self.clock.t += HOUR
        self.assertEqual(checker.check(token), "main_gone")
        self.fake.add({"services": [dict(TORBOX)], "presets": []}, PASSWORD)   # another config doesn't bring it back
        self.clock.t += 24 * HOUR
        self.assertEqual(checker.check(token), "main_gone")

    def test_an_unreachable_aiostreams_is_a_note_after_three_checks(self):
        uuid, token = self.sign_up()
        checker = self.checker()
        self.dead = True
        for n in range(2):
            self.assertIsNone(checker.check(token))
            self.clock.t += 24 * HOUR
        self.assertEqual(checker.check(token), "unreachable")
        self.dead = False
        self.clock.t += 24 * HOUR
        self.assertIsNone(checker.check(token))
        self.assertEqual(self.record(token)["unreachable"], 0)

    def test_checks_start_in_the_background_at_most_once_a_period(self):
        uuid, token = self.sign_up()
        checker = self.checker()
        self.assertFalse(checker.maybe_start(token))                      # just signed up: the next check is a day away
        self.clock.t += 24 * HOUR
        self.assertTrue(checker.maybe_start(token))
        checker.join(5)
        self.assertEqual(self.record(token)["next_check"], self.clock.t + 24 * HOUR)
        self.assertFalse(checker.maybe_start(token))                      # not due yet
        self.clock.t += 24 * HOUR
        self.assertTrue(checker.maybe_start(token))
        checker.join(5)

    def test_a_check_does_not_hold_up_the_request_that_started_it(self):
        uuid, token = self.sign_up()
        checker = self.checker()
        self.fake.fault, self.fake.hang_s = "hang", 1.0
        self.clock.t += 24 * HOUR
        t0 = time.time()
        self.assertTrue(checker.maybe_start(token))
        self.assertLess(time.time() - t0, 0.5)
        self.assertFalse(checker.maybe_start(token))                      # one check per person at a time
        checker.join(10)

    def test_off_with_zero_hours(self):
        uuid, token = self.sign_up()
        before = len(self.fake.requests)
        self.assertFalse(self.checker(SUBSYNC_RECHECK_HOURS="0").maybe_start(token))
        self.assertEqual(len(self.fake.requests), before)

    def test_one_check_at_a_time_per_aiostreams(self):
        tokens = [self.sign_up()[1] for _ in range(3)]
        checker = self.checker()
        self.fake.fault, self.fake.hang_s = "hang", 0.2
        self.fake.max_inflight = 0
        threads = [threading.Thread(target=checker.check, args=(t,)) for t in tokens]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(self.fake.max_inflight, 1)

    def test_a_refresh_during_a_check_wins(self):
        uuid, token = self.sign_up()
        checker = self.checker()
        real_look = checker._look

        def look_then_refresh(tok, record):
            found = real_look(tok, record)
            self.signup.refresh(self.s, self.users, token, {"password": PASSWORD}, api_factory=self.api, timeout=5)
            return found
        del self.fake.configs[self.record(token)["sync_uuid"]]
        checker._look = look_then_refresh
        checker.check(token)
        record = self.record(token)
        self.assertIsNone(record.get("state"))          # the refresh made a new config; the check's "gone" is old news
        self.assertIn(record["sync_uuid"], self.fake.configs)

    def test_a_config_the_check_made_is_removed_when_a_refresh_wins(self):
        uuid, token = self.sign_up(keep=True)
        checker = self.checker()
        real_look = checker._look

        def look_then_refresh(tok, record):
            found = real_look(tok, record)          # the kept password makes a new config for the gone one
            self.signup.refresh(self.s, self.users, token, {"password": PASSWORD, "keep_password": "on"},
                                api_factory=self.api, timeout=5)
            return found
        del self.fake.configs[self.record(token)["sync_uuid"]]
        checker._look = look_then_refresh
        checker.check(token)
        sync_configs = [k for k, v in self.fake.configs.items() if k != uuid]
        self.assertEqual(sync_configs, [self.record(token)["sync_uuid"]])   # only the refresh's config is left

    def test_both_configs_gone_says_sync_gone_at_once(self):
        uuid, token = self.sign_up()
        del self.fake.configs[uuid]
        del self.fake.configs[self.record(token)["sync_uuid"]]
        self.assertEqual(self.checker().check(token), "sync_gone")
        self.clock.t += HOUR
        self.assertEqual(self.checker().check(token), "main_gone")

    def test_a_gone_config_is_made_again_without_trying_to_save_it(self):
        uuid, token = self.sign_up(keep=True)
        del self.fake.configs[self.record(token)["sync_uuid"]]
        puts = sum(1 for m, _ in self.fake.requests if m == "PUT")
        self.checker().check(token)
        self.assertEqual(sum(1 for m, _ in self.fake.requests if m == "PUT"), puts)

    def test_unknown_token(self):
        from subsync.users import new_token
        self.assertIsNone(self.checker().check(new_token()))
        self.assertFalse(self.checker().maybe_start(new_token()))

    # -- with a kept password (opt-in) ------------------------------------------------------------------------------
    def test_a_kept_password_rebuilds_aiosubtitles_config_by_itself(self):
        uuid, token = self.sign_up(keep=True)
        main = self.fake.configs[uuid]["config"]
        main["services"].append(dict(REALDEBRID))
        main["presets"].insert(0, dict(COMET))
        main_before = json.dumps(main, sort_keys=True)
        self.assertIsNone(self.checker().check(token))
        sync = self.fake.configs[self.record(token)["sync_uuid"]]["config"]
        self.assertIn("comet", [p["type"] for p in sync["presets"]])
        self.assertEqual([s["id"] for s in sync["services"]], ["torbox"])           # Real-Debrid left out
        self.assertEqual(json.dumps(self.fake.configs[uuid]["config"], sort_keys=True), main_before)   # theirs untouched
        record = self.record(token)
        self.assertEqual(record["synced_at"], self.clock.t)
        self.assertEqual(self.changed, [])      # the same config, new content: the worker reads it on its next request

    def test_an_unchanged_config_is_not_saved_again(self):
        uuid, token = self.sign_up(keep=True)
        puts = sum(1 for m, _ in self.fake.requests if m == "PUT")
        self.assertIsNone(self.checker().check(token))
        self.assertEqual(sum(1 for m, _ in self.fake.requests if m == "PUT"), puts)
        self.assertEqual(self.changed, [])

    def test_a_kept_password_remakes_a_gone_sync_config(self):
        uuid, token = self.sign_up(keep=True)
        old = self.record(token)["sync_uuid"]
        del self.fake.configs[old]
        self.assertIsNone(self.checker().check(token))
        new = self.record(token)["sync_uuid"]
        self.assertNotEqual(new, old)
        self.assertIn(new, self.fake.configs)

    def test_a_refused_kept_password_is_dropped_and_asks_for_a_refresh(self):
        uuid, token = self.sign_up(keep=True)
        self.fake.configs[uuid]["password"] = "changed-by-them"
        self.assertEqual(self.checker().check(token), "password_refused")
        self.assertNotIn("auto_password", self.record(token))

    def test_a_config_that_can_no_longer_be_used_says_why(self):
        uuid, token = self.sign_up(keep=True)
        self.fake.configs[uuid]["config"]["services"] = []      # nothing left to read through (Real-Debrid alone now
        self.assertEqual(self.checker().check(token), "cannot_rebuild")   # means stored copies: test_stored_copies)
        self.assertIn("can't find any file", self.record(token)["state_detail"])

    def test_the_kept_password_is_never_in_clear_on_disk_or_in_the_log(self):
        uuid, token = self.sign_up(keep=True)
        self.checker().check(token)
        self.users.close()
        data = b"".join(open(os.path.join(self.dir, f), "rb").read() for f in os.listdir(self.dir)
                        if f.startswith("users.sqlite3"))
        from subsync.users import Users
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes(), clock=self.clock)
        self.assertNotIn(PASSWORD.encode(), data)
        self.assertFalse([line for line in self.logs.lines if PASSWORD in line])


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class RefreshTest(Base):
    """Refresh after the check found something: a new sync config when it is gone, a new config link, the kept
    password ticked or unticked."""

    def test_refresh_makes_a_new_sync_config_when_it_is_gone(self):
        uuid, token = self.sign_up()
        old = self.record(token)["sync_uuid"]
        del self.fake.configs[old]
        self.signup.refresh(self.s, self.users, token, {"password": PASSWORD}, api_factory=self.api, timeout=5)
        record = self.record(token)
        self.assertNotEqual(record["sync_uuid"], old)
        self.assertIn(record["sync_uuid"], self.fake.configs)
        self.assertIsNone(record.get("state"))

    def test_refresh_clears_the_state_and_sets_the_next_check(self):
        uuid, token = self.sign_up()
        record = self.record(token)
        record.update(state="main_gone", unreachable=2)
        self.users.update(token, record)
        self.signup.refresh(self.s, self.users, token, {"password": PASSWORD}, api_factory=self.api, timeout=5)
        record = self.record(token)
        self.assertIsNone(record.get("state"))
        self.assertEqual(record["unreachable"], 0)
        self.assertEqual(record["next_check"], self.clock.t + 24 * HOUR)

    def test_refresh_with_a_new_link_on_the_same_aiostreams(self):
        uuid, token = self.sign_up()
        sync_uuid = self.record(token)["sync_uuid"]
        new_main = self.fake.add({"services": [dict(TORBOX)], "presets": [dict(COMET)]}, "another-password")
        self.signup.refresh(self.s, self.users, token,
                            {"password": "another-password",
                             "link": f"{INSTANCE}/stremio/{new_main}/ENCRYPTED/manifest.json"},
                            api_factory=self.api, timeout=5)
        record = self.record(token)
        self.assertEqual(record["main_uuid"], new_main)
        self.assertEqual(record["sync_uuid"], sync_uuid)                  # the same sync config, rebuilt
        self.assertIn("comet", [p["type"] for p in self.fake.configs[sync_uuid]["config"]["presets"]])

    def test_refresh_with_a_link_on_another_aiostreams_moves_the_sync_config(self):
        uuid, token = self.sign_up()
        old_sync = self.record(token)["sync_uuid"]
        new_main = self.fake.add({"services": [dict(TORBOX)], "presets": [dict(COMET)]}, PASSWORD)
        self.signup.refresh(self.s, self.users, token,
                            {"password": PASSWORD, "link": f"{OTHER}/stremio/{new_main}/ENCRYPTED/manifest.json"},
                            api_factory=self.api, timeout=5)
        record = self.record(token)
        self.assertEqual((record["instance"], record["main_uuid"]), (OTHER, new_main))
        self.assertNotEqual(record["sync_uuid"], old_sync)
        self.assertTrue(record["sync_link"].startswith(f"{OTHER}/stremio/{record['sync_uuid']}/"))
        self.assertNotIn(old_sync, self.fake.configs)                     # the old one removed

    def test_a_failed_refresh_leaves_no_new_config_behind(self):
        uuid, token = self.sign_up()
        new_main = self.fake.add({"services": [dict(TORBOX)], "presets": [dict(COMET)]}, PASSWORD)
        before = set(self.fake.configs)
        from unittest import mock
        with mock.patch.object(self.users, "update", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.signup.refresh(self.s, self.users, token,
                                    {"password": PASSWORD, "link": f"{OTHER}/stremio/{new_main}/ENCRYPTED/manifest.json"},
                                    api_factory=self.api, timeout=5)
        self.assertEqual(set(self.fake.configs), before)

    def test_a_wrong_new_link_changes_nothing(self):
        uuid, token = self.sign_up()
        before = dict(self.record(token))
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup.refresh(self.s, self.users, token, {"password": PASSWORD, "link": "http://not-https/x"},
                                api_factory=self.api, timeout=5)
        self.assertEqual(e.exception.field, "link")
        self.assertEqual(self.record(token), before)

    def test_refresh_ticked_keeps_the_password_and_unticked_forgets_it(self):
        uuid, token = self.sign_up()
        self.signup.refresh(self.s, self.users, token, {"password": PASSWORD, "keep_password": "on"},
                            api_factory=self.api, timeout=5)
        self.assertEqual(self.record(token)["auto_password"], PASSWORD)
        self.signup.refresh(self.s, self.users, token, {"password": PASSWORD}, api_factory=self.api, timeout=5)
        self.assertNotIn("auto_password", self.record(token))

    def test_forget_password_at_once(self):
        uuid, token = self.sign_up(keep=True)
        before = len(self.fake.requests)
        self.signup.forget_password(self.users, token)
        self.assertNotIn("auto_password", self.record(token))
        self.assertEqual(len(self.fake.requests), before)                  # nothing asked of AIOStreams


if __name__ == "__main__":
    unittest.main()
