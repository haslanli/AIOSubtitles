"""Public mode: a setup whose only debrid service is Real-Debrid signs up for stored copies only: no sync config
is made and none of its files is ever read; its subtitle lists get the copies other people's syncs already made for
the same release. Refresh and the daily check (with a kept password) move a person both ways."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import TempDir  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from test_recheck import (HAVE_CRYPTOGRAPHY, HOUR, INSTANCE, KEY, PASSWORD, REALDEBRID, TORBOX, TORRENTIO,  # noqa: E402
                          Base)

OSV3 = {"type": "opensubtitles-v3-plus", "instanceId": "osp", "enabled": True,
        "options": {"name": "OpenSubtitles V3+", "resources": ["subtitles"]}}
SYNC_FIELDS = ("sync_uuid", "sync_link", "sync_password", "sync_fp")


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class StoredCopiesTest(Base):
    def sign_up_rd(self, keep=False, **extra):
        viewer = {"services": [dict(REALDEBRID)], "presets": [dict(TORRENTIO), dict(OSV3)]}
        uuid = self.fake.add(viewer, PASSWORD)
        form = dict({"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                     "languages": "English", "add_addon": "on"}, **extra)
        if keep:
            form["keep_password"] = "on"
        return uuid, self.signup.signup(self.s, self.users, form, api_factory=self.api, timeout=5)

    def refresh(self, token, **extra):
        return self.signup.refresh(self.s, self.users, token, dict({"password": PASSWORD, "add_addon": "on"}, **extra),
                                   api_factory=self.api, timeout=5)

    def creates(self):
        return [r for r in self.fake.requests if r[0] == "POST"]

    def assert_stored_copies_only(self, record):
        self.assertIs(record.get("store_only"), True)
        for name in SYNC_FIELDS:
            self.assertNotIn(name, record)

    def names(self, uuid):
        return [p["options"]["name"] for p in self.fake.configs[uuid]["config"]["presets"]]

    # -- sign-up -----------------------------------------------------------------------------------------------------
    def test_signup_makes_no_sync_config_and_keeps_a_stored_copies_record(self):
        uuid, result = self.sign_up_rd()
        self.assertTrue(result.store_only)
        self.assertIsNone(result.warning)
        self.assertEqual(list(self.fake.configs), [uuid])          # only their own config: nothing made on their AIOStreams
        self.assertEqual(self.creates(), [])
        self.assert_stored_copies_only(self.record(result.token))
        self.assertTrue(result.added)                              # their apps need the entry to see the copies
        self.assertIn("AIOSubtitles", self.names(uuid))
        self.assertEqual(self.record(result.token)["languages"], ["eng"])

    def test_unticked_their_config_stays_as_it_is(self):
        uuid, result = self.sign_up_rd(add_addon="")
        self.assertTrue(result.store_only)
        self.assertFalse(result.added)
        self.assertEqual(self.fake.writes(), [])

    def test_the_clean_up_is_left_out_since_nothing_is_searched_for_them(self):
        uuid, result = self.sign_up_rd(clean_up="on")
        self.assertIn("OpenSubtitles V3+", self.names(uuid))
        self.assertIn("AIOSubtitles", self.names(uuid))

    def test_real_debrid_beside_torbox_is_still_a_full_sign_up(self):
        viewer = {"services": [dict(TORBOX), dict(REALDEBRID)], "presets": [dict(TORRENTIO)]}
        uuid = self.fake.add(viewer, PASSWORD)
        result = self.signup.signup(self.s, self.users, {"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json",
                                                         "password": PASSWORD, "languages": "English"},
                                    api_factory=self.api, timeout=5)
        self.assertFalse(result.store_only)
        self.assertNotIn("store_only", self.record(result.token))
        self.assertIn(self.record(result.token)["sync_uuid"], self.fake.configs)

    # -- refresh -----------------------------------------------------------------------------------------------------
    def test_refresh_after_adding_a_service_makes_the_sync_config(self):
        uuid, result = self.sign_up_rd()
        self.fake.configs[uuid]["config"]["services"].append(dict(TORBOX))
        refreshed = self.refresh(result.token)
        self.assertFalse(refreshed.store_only)
        self.assertFalse(refreshed.to_store_only)
        record = self.record(result.token)
        self.assertNotIn("store_only", record)
        sync = self.fake.configs[record["sync_uuid"]]["config"]
        self.assertEqual([s["id"] for s in sync["services"]], ["torbox"])

    def test_refresh_when_their_config_became_real_debrid_only_removes_the_sync_config(self):
        uuid, token = self.sign_up()
        sync_uuid = self.record(token)["sync_uuid"]
        self.fake.configs[uuid]["config"]["services"] = [dict(REALDEBRID)]
        refreshed = self.refresh(token)
        self.assertTrue(refreshed.store_only)
        self.assertTrue(refreshed.to_store_only)
        self.assert_stored_copies_only(self.record(token))
        self.assertNotIn(sync_uuid, self.fake.configs)
        self.assertIsNone(self.record(token).get("state"))

    def test_refresh_staying_real_debrid_only_makes_nothing(self):
        uuid, result = self.sign_up_rd()
        refreshed = self.refresh(result.token)
        self.assertTrue(refreshed.store_only)
        self.assertFalse(refreshed.to_store_only)
        self.assertEqual(self.creates(), [])
        self.assert_stored_copies_only(self.record(result.token))

    def test_refresh_with_a_new_link_on_another_aiostreams_while_stored_copies_only(self):
        uuid, result = self.sign_up_rd()
        other = self.fake.add({"services": [dict(REALDEBRID)], "presets": [dict(TORRENTIO)]}, PASSWORD)
        refreshed = self.refresh(result.token, link=f"https://other-aios.test/stremio/{other}/ENC/manifest.json")
        self.assertTrue(refreshed.store_only)
        record = self.record(result.token)
        self.assertEqual((record["instance"], record["main_uuid"]), ("https://other-aios.test", other))
        self.assertEqual(self.creates(), [])

    # -- the daily check -------------------------------------------------------------------------------------------
    def test_the_daily_check_leaves_stored_copies_alone_without_a_kept_password(self):
        uuid, result = self.sign_up_rd()
        before = len(self.fake.requests)
        checker = self.checker()
        self.clock.t += 25 * HOUR
        self.assertFalse(checker.maybe_start(result.token))
        checker.join(5)
        self.assertEqual(len(self.fake.requests), before)

    def test_with_a_kept_password_the_daily_check_moves_both_ways(self):
        uuid, result = self.sign_up_rd(keep=True)
        token = result.token
        checker = self.checker()
        self.clock.t += 25 * HOUR
        self.assertIsNone(checker.check(token))                  # still Real-Debrid only: nothing made
        self.assert_stored_copies_only(self.record(token))
        self.assertEqual(self.creates(), [])
        self.assertEqual(self.changed, [])

        self.fake.configs[uuid]["config"]["services"].append(dict(TORBOX))
        self.clock.t += 25 * HOUR
        self.assertIsNone(checker.check(token))                  # a service added: its own sync config now
        record = self.record(token)
        self.assertNotIn("store_only", record)
        self.assertIn(record["sync_uuid"], self.fake.configs)
        self.assertEqual(self.changed, [token])
        sync_uuid = record["sync_uuid"]

        self.fake.configs[uuid]["config"]["services"] = [dict(REALDEBRID)]
        self.clock.t += 25 * HOUR
        self.assertIsNone(checker.check(token))                  # Real-Debrid only again: the sync config goes
        self.assert_stored_copies_only(self.record(token))
        self.assertNotIn(sync_uuid, self.fake.configs)
        self.assertEqual(self.changed, [token, token])

    def test_a_record_with_a_sync_config_whose_config_became_real_debrid_only_without_a_password_is_unchanged(self):
        uuid, token = self.sign_up()
        self.fake.configs[uuid]["config"]["services"] = [dict(REALDEBRID)]
        checker = self.checker()
        self.clock.t += 25 * HOUR
        checker.check(token)                                     # without the password it can't tell: nothing moves
        record = self.record(token)
        self.assertNotIn("store_only", record)
        self.assertIn(record["sync_uuid"], self.fake.configs)

    # -- delete ------------------------------------------------------------------------------------------------------
    def test_delete_has_no_sync_config_to_remove(self):
        uuid, result = self.sign_up_rd()
        outcome = self.signup.delete(self.s, self.users, result.token, {"password": ""}, api_factory=self.api,
                                     timeout=5)
        self.assertTrue(outcome["sync_removed"])                 # nothing of AIOSubtitles left on their AIOStreams
        self.assertIsNone(self.record(result.token))
        self.assertEqual([r for r in self.fake.requests if r[0] == "DELETE"], [])

    def test_delete_with_the_password_takes_aiosubtitles_out_of_their_config(self):
        uuid, result = self.sign_up_rd()
        outcome = self.signup.delete(self.s, self.users, result.token, {"password": PASSWORD}, api_factory=self.api,
                                     timeout=5)
        self.assertEqual(outcome, {"sync_removed": True, "addon_removed": True})
        self.assertNotIn("AIOSubtitles", self.names(uuid))


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class StoredCopiesServedTest(unittest.TestCase):
    """A stored-copies-only person's requests are answered from the shared store, by the request's own file details,
    and never start a read."""

    def setUp(self):
        from subsync.public import Pool
        from subsync.store import Store
        from subsync.users import Users, new_token
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                    "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_EXTRA_SOURCES": "",
                    "SUBSYNC_RECHECK_HOURS": "0"}
        self.s = Settings(self.env)
        self.store = Store(self.dir)
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes())
        self.pool = Pool(self.s, self.env, self.store, self.users, start_workers=False)
        self.token = new_token()
        self.users.create(self.token, {"instance": INSTANCE, "main_uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                       "languages": ["eng"], "version": 1, "store_only": True})
        key = "0123456789abcdef-1000"
        self.store.put(key, "a" * 64, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt",
                       candidate_id="os-1")
        self.store.alias("hash:0123456789abcdef|1000", key)
        self.store.alias("name:a.mkv|1000", key)

    def tearDown(self):
        self.pool.close()
        self.users.close()
        self.tmp.__exit__(None, None, None)

    def test_copies_by_hash_and_by_name_and_size(self):
        app = self.pool.app(self.token)
        for extras in ("videoHash=0123456789abcdef&videoSize=1000", "filename=a.mkv&videoSize=1000"):
            with self.subTest(extras=extras):
                subs = app.subtitles("movie", "tt1", extras)
                self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs],
                                 [("eng", "English (synced)", "os-1")])

    def test_an_unknown_file_starts_nothing(self):
        app = self.pool.app(self.token)
        self.assertEqual(app.subtitles("movie", "tt2", "filename=b.mkv&videoSize=2000"), [])
        self.assertEqual(app.worker.jobs, {})
        self.assertEqual(self.pool.active(), 0)                  # no worker of their own, ever
        self.assertEqual(app.s.source_manifest, "")              # and nothing to read through


if __name__ == "__main__":
    unittest.main()


class TextsTest(unittest.TestCase):
    """The approved words for stored copies, word for word."""

    def test_before_you_start(self):
        from subsync import explain
        self.assertEqual(explain.PUBLIC_BEFORE[1], "A debrid service in that config other than Real-Debrid, such as "
                         "TorBox. With Real-Debrid alone, you get only the copies already synced for someone else.")

    def test_the_stored_copies_note(self):
        from subsync import explain
        from subsync.realdebrid import REASON
        self.assertEqual(explain.PUBLIC_STORE_ONLY, (
            "Stored copies only",
            f"Real-Debrid is the only debrid service in your AIOStreams config, and AIOSubtitles never uses it, because "
            f"{REASON}. So it reads none of your files: for each file you play, it offers the synced copies already made "
            "for someone who played the same release, when there are any. To get your own synced copies, add another "
            "service, such as TorBox, to your config, then press Refresh on your settings page."))

    def test_after_a_refresh_that_moved_them(self):
        from subsync import explain
        self.assertEqual(explain.PUBLIC_TEXTS["to_stored"], "Refreshed: Real-Debrid is now the only debrid service in "
                         "your config, so AIOSubtitles removed its own config from your AIOStreams and offers stored "
                         "copies only.")

    def test_the_rule_no_longer_says_sign_up_stops(self):
        from subsync import explain
        rule = dict(explain.RULES)["Public mode: Real-Debrid is left out"]
        self.assertNotIn("sign-up stops", rule)
        self.assertIn("With Real-Debrid as your only debrid service, AIOSubtitles reads nothing for you: you get only "
                      "the copies already synced for someone else who played the same release.", rule)
