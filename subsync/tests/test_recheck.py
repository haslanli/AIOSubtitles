"""Public mode: each person's AIOStreams checked again about once a day while they use the service, against a stand-in
AIOStreams on this machine. Without a kept password the check sees only what needs no password (does their own config
still exist; AIOSubtitles' own config, with its own password) and repairs what it can; with the password a person chose
to let AIOSubtitles keep, it also rebuilds AIOSubtitles' config from theirs. It never changes the person's own config."""
import json
import os
import threading
import time
import unittest
from unittest import mock

from helpers import (files_bytes, HAVE_CRYPTOGRAPHY, HOUR, INSTANCE, NEEDS, OTHER, PASSWORD, REALDEBRID,
                     RecheckHarness, TORBOX)

COMET = {"type": "comet", "instanceId": "cmt", "enabled": True, "options": {"name": "Comet"}}


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class RecheckTest(RecheckHarness, unittest.TestCase):
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

    def test_aiostreams_own_words_are_never_taken_for_an_unreachable_aiostreams(self):
        # A refused config read whose words hold "Can't reach" (AIOStreams naming a service it can't reach)
        # was counted as their AIOStreams not reached, and three such checks said "unreachable"
        from subsync.recheck import UNREACHABLE_CHECKS
        uuid, token = self.sign_up()
        make = self.api

        def refusing(base, timeout):
            api = make(base, timeout)

            def get_config(config_id, password):
                refusal = {"success": False, "error": {"code": "INTERNAL", "message": "Can't reach TorBox"}}
                raise api._error(500, refusal, "Reading the config")
            api.get_config = get_config
            return api
        self.api = refusing
        checker = self.checker()
        for _ in range(UNREACHABLE_CHECKS):
            checker.check(token)
            self.clock.t += 24 * HOUR
        record = self.record(token)
        self.assertEqual(record["unreachable"], 0)
        self.assertNotEqual(record.get("state"), "unreachable")

    def test_a_slow_aiostreams_is_not_counted_as_unreachable(self):
        # Asked, and its answer too slow to come within the check's wait: it was reached, so it is no check
        # that couldn't reach it
        uuid, token = self.sign_up()
        checker = self.checker()
        checker.timeout = 1
        self.fake.fault, self.fake.hang_s = "hang", 1.5
        self.assertIsNone(checker.check(token))
        self.assertEqual(self.record(token)["unreachable"], 0)

    def test_a_garbled_answer_is_not_counted_as_unreachable(self):
        # It answered, so it is reached: an error, never three of them making the page say "unreachable";
        # Three of them say it answered with an error
        uuid, token = self.sign_up()
        checker = self.checker()
        self.fake.fault = "garbled"
        states = []
        for _ in range(3):
            states.append(checker.check(token))
            self.clock.t += 24 * HOUR
        self.assertEqual(states, [None, None, "aiostreams_errors"])
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
        t0 = time.monotonic()
        self.assertTrue(checker.maybe_start(token))
        self.assertLess(time.monotonic() - t0, 0.5)
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
        self.clock.t += HOUR                          # so each record shows its own check ran
        outcomes = {}

        def check(token):                            # a check that failed or never ran also kept it at 1
            try:
                outcomes[token] = checker.check(token)
            except Exception as e:
                outcomes[token] = e
        # daemon threads: one stuck on the per-AIOStreams lock fails the test, never holds up the run's exit
        threads = [threading.Thread(target=check, args=(t,), daemon=True) for t in tokens]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual([t.is_alive() for t in threads], [False] * 3)   # none still waits for the lock
        self.assertEqual(outcomes, dict.fromkeys(tokens))          # all three ran to the end, each "all well"
        self.assertEqual([self.record(t)["checked_at"] for t in tokens], [self.clock.t] * 3)
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

    def test_a_config_the_check_made_is_removed_when_the_record_was_deleted_meanwhile(self):
        # They delete their data while their check's kept password makes a new config for the gone one
        uuid, token = self.sign_up(keep=True)
        checker = self.checker()
        real_look = checker._look
        made = []

        def look_then_delete(tok, record):
            found = real_look(tok, record)
            made.append(found["fields"]["sync_uuid"])
            self.signup.delete(self.s, self.users, token, {"password": ""}, api_factory=self.api, timeout=5)
            return found
        del self.fake.configs[self.record(token)["sync_uuid"]]
        checker._look = look_then_delete
        self.assertIsNone(checker.check(token))
        self.assertIsNone(self.record(token))
        self.assertTrue(made[0])
        self.assertEqual([k for k in self.fake.configs if k != uuid], [])   # nothing of AIOSubtitles' is left

    def test_configs_are_removed_once_the_record_is_let_go(self):
        # A delete on a slow AIOStreams takes up to its timeout; the person's own actions don't wait for it
        from subsync import recheck
        uuid, token = self.sign_up(keep=True)
        checker = self.checker()
        real_look = checker._look

        def look_then_refresh(tok, record):
            found = real_look(tok, record)
            self.signup.refresh(self.s, self.users, token, {"password": PASSWORD, "keep_password": "on"},
                                api_factory=self.api, timeout=5)
            return found
        del self.fake.configs[self.record(token)["sync_uuid"]]
        checker._look = look_then_refresh
        held = []

        def drop_sync(api, fields):
            held.append(self.users.locked(token).locked())
            real_drop(api, fields)
        real_drop = recheck.drop_sync
        with mock.patch.object(recheck, "drop_sync", drop_sync):
            checker.check(token)
        self.assertEqual(held, [False])
        self.assertEqual([k for k in self.fake.configs if k != uuid], [self.record(token)["sync_uuid"]])

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

    def test_the_notice_names_aiosubtitles_not_subsync(self):
        # The daily check's notice says the public service's name
        uuid, token = self.sign_up(keep=True)
        self.fake.configs[uuid]["config"]["services"] = []
        self.assertEqual(self.checker().check(token), "cannot_rebuild")
        self.assertEqual(self.record(token)["state_detail"],
                         "AIOSubtitles can't find any file it could read through your AIOStreams: no debrid service "
                         "(AIOSubtitles reads files from debrid services only, not Usenet or torrents).")

    def test_a_refused_config_is_aiosubtitles_fault_and_needs_nothing_from_the_person(self):
        # AIOStreams refusing the config AIOSubtitles made from theirs is AIOSubtitles' fault, said in's
        # words on the settings page only: no "action needed" entry in their subtitle lists
        from subsync import explain
        from subsync.addon import App
        uuid, token = self.sign_up(keep=True)
        self.fake.configs[uuid]["config"]["presets"].insert(0, dict(COMET))   # a change the rebuild follows,
        self.fake.refusal = lambda config: "Invalid preset options"           # which their AIOStreams refuses
        self.assertEqual(self.checker().check(token), "config_refused")
        record = self.record(token)
        self.assertEqual(record["state_detail"], "Invalid preset options")
        state = explain.PUBLIC_STATES["config_refused"]
        self.assertEqual(state.title, "Your AIOStreams didn't accept AIOSubtitles' new config")
        self.assertEqual(explain.state_text("config_refused", record["state_detail"]),
                         "AIOSubtitles tried to follow a change in your AIOStreams config, and your AIOStreams didn't "
                         "accept the config it made from yours. This is a fault in AIOSubtitles, not in your config: "
                         "nothing needs doing on your side. Until it is fixed, AIOSubtitles keeps using the config it "
                         "made before. AIOStreams' reason: \"Invalid preset options\"")
        self.assertIsNone(App(self.s, None, None, None, state="config_refused").action())
        self.assertEqual(App(self.s, None, None, None, state="cannot_rebuild").action(), "cannot_rebuild")   # unlike
        del self.fake.refusal                                                 # accepted again: all well
        self.clock.t += 24 * HOUR
        self.assertIsNone(self.checker().check(token))
        self.assertNotIn("state_detail", self.record(token))

    def test_a_refused_config_is_one_warning_line_for_the_operator(self):
        # When the state becomes config_refused, one WARNING with AIOStreams' reason alone, nothing
        # about the person, their link or a key; still refused at the next check, no second line
        uuid, token = self.sign_up(keep=True)
        record = self.record(token)
        self.fake.configs[uuid]["config"]["presets"].insert(0, dict(COMET))
        self.fake.refusal = lambda config: "Invalid preset options"
        with self.assertLogs("subsync.recheck", "WARNING") as logged:
            self.assertEqual(self.checker().check(token), "config_refused")
        self.assertEqual(logged.output, ['WARNING:subsync.recheck:AIOStreams refused the config AIOSubtitles made at a '
                                         'daily check: "Invalid preset options"'])
        for private in (token, uuid, record["sync_uuid"], record["sync_link"], record["sync_password"], PASSWORD,
                        INSTANCE, "tb-key"):
            self.assertNotIn(private, logged.output[0])
        self.clock.t += 24 * HOUR
        with self.assertNoLogs("subsync.recheck", "WARNING"):
            self.assertEqual(self.checker().check(token), "config_refused")

    def test_an_error_answer_is_a_note_after_three_checks_in_a_row_never_action_needed(self):
        # A passing error (busy, a fault, an answer it can't read) is counted as "couldn't reach" is
        from subsync import explain
        from subsync.addon import App
        from subsync.recheck import UNREACHABLE_CHECKS
        uuid, token = self.sign_up(keep=True)
        self.fake.configs[uuid]["config"]["presets"].insert(0, dict(COMET))
        self.fake.refuse_save_for.add(self.record(token)["sync_uuid"])       # its save answers a server error
        checker = self.checker()
        for n in range(1, UNREACHABLE_CHECKS):
            self.assertIsNone(checker.check(token))
            self.assertEqual(self.record(token)["failed"], n)
            self.clock.t += 24 * HOUR
        self.assertEqual(checker.check(token), "aiostreams_errors")
        state = explain.PUBLIC_STATES["aiostreams_errors"]
        self.assertEqual(UNREACHABLE_CHECKS, 3)                               # as its text says
        self.assertEqual((state.title, state.text), (
            "Your AIOStreams answered with an error",
            "The last three checks got an error from your AIOStreams or couldn't reach it (it was busy or had a "
            "fault), so AIOSubtitles couldn't finish checking your config. It keeps using the config it made before "
            "and tries again at the next check; nothing needs doing."))
        self.assertIsNone(App(self.s, None, None, None, state="aiostreams_errors").action())
        self.fake.refuse_save_for.clear()
        self.clock.t += 24 * HOUR
        self.assertIsNone(checker.check(token))
        self.assertNotIn("failed", self.record(token))

    def failing_reads(self):
        """Every read of AIOSubtitles' own configs answers a server error (a busy or faulty AIOStreams), while `dead`
        still makes it answer nothing; `del self.api` ends it."""
        make = self.api

        def failing(base, timeout):
            api = make(base, timeout)

            def get_config(config_id, password):
                raise api._error(500, {"success": False, "error": {"code": "INTERNAL", "message": "busy"}},
                                 "Reading the config")
            if not self.dead:
                api.get_config = get_config
            return api
        self.api = failing

    def test_an_error_answer_to_the_config_reads_counts_and_keeps_the_note(self):
        # With no kept password these reads are the whole check, and their error answers counted for nothing:
        # every such check was "all well", and it cleared the note an earlier error had given
        uuid, token = self.sign_up()
        self.failing_reads()
        checker = self.checker()
        for n in (1, 2):
            self.assertIsNone(checker.check(token))
            self.assertEqual(self.record(token)["failed"], n)
            self.clock.t += 24 * HOUR
        self.assertEqual(checker.check(token), "aiostreams_errors")
        self.clock.t += 24 * HOUR
        self.assertEqual(checker.check(token), "aiostreams_errors")          # a fourth keeps it
        self.assertEqual(self.record(token)["unreachable"], 0)

    def test_checks_that_fail_either_way_count_together(self):
        # A check that couldn't reach their AIOStreams and one it answered with an error reset each other's
        # count, so an AIOStreams down one day and failing the next was never noted. One count now; the "couldn't
        # reach" note only when none of the last three reached it
        uuid, token = self.sign_up()
        self.failing_reads()
        checker = self.checker()
        states = []
        for dead in (True, False, True, True, True, False, False):
            self.dead = dead
            states.append(checker.check(token))
            self.clock.t += 24 * HOUR
        self.assertEqual(states, [None, None, "aiostreams_errors", "aiostreams_errors", "unreachable",
                                  "aiostreams_errors", "aiostreams_errors"])
        del self.api                                                     # their AIOStreams answers again
        self.assertIsNone(self.checker().check(token))
        record = self.record(token)
        self.assertEqual((record.get("state"), record.get("failed"), record["unreachable"]), (None, None, 0))

    def test_the_reason_a_config_cant_be_followed_ends_its_sentence(self):
        # Case 1 keeps "can't be followed as it is" (action needed), its reason always a sentence of its own
        from subsync import recheck
        uuid, token = self.sign_up(keep=True)
        unusable = self.signup.SignupError("AIOSubtitles can't find any file it could read through your AIOStreams",
                                           unusable=True)
        with mock.patch.object(recheck, "rebuild", side_effect=unusable):
            self.assertEqual(self.checker().check(token), "cannot_rebuild")
        self.assertEqual(self.record(token)["state_detail"],
                         "AIOSubtitles can't find any file it could read through your AIOStreams.")

    def test_aiostreams_reason_is_one_short_line_in_the_log_and_the_record(self):
        # A person's own AIOStreams-like server refused the rebuilt config with line breaks and 200,000
        # characters: a line of its own in the operator's log, and a record and settings note of that size
        from subsync.aiosconfig import ERROR_EXCERPT
        uuid, token = self.sign_up(keep=True)
        self.fake.configs[uuid]["config"]["presets"].insert(0, dict(COMET))
        forged = "x\r\n2026-11-19 00:00:00,000 ERROR subsync.public forged\u2028line\x1b[31m " + "A" * 200_000
        self.fake.refusal = lambda config: forged
        with self.assertLogs("subsync.recheck", "WARNING") as logged:
            self.assertEqual(self.checker().check(token), "config_refused")
        detail = self.record(token)["state_detail"]
        self.assertTrue(detail.startswith("x 2026-11-19 00:00:00,000 ERROR subsync.public forged line [31m AAA"),
                        detail)
        self.assertTrue(detail.endswith("A…"))
        self.assertEqual(len(detail), ERROR_EXCERPT)
        self.assertEqual(logged.output, ['WARNING:subsync.recheck:AIOStreams refused the config AIOSubtitles made at a '
                                         f'daily check: "{detail}"'])
        self.assertNotRegex(logged.output[0], "[\x00-\x1f\x7f-\x9f\u2028\u2029]")

    def test_a_changed_link_is_repaired_whatever_the_rebuild_found(self):
        # AIOStreams gave AIOSubtitles' config a new link while the rebuild of the same check failed; the
        # record kept the old link, which may no longer open, while the state's text says AIOSubtitles keeps using the
        # config it made before
        for state in ("config_refused", "cannot_rebuild", "password_refused", None):   # None: a first error answer
            with self.subTest(state=state):
                self.fake.__dict__.pop("refusal", None)       # each case on its own: nothing refused before it
                self.fake.refuse_save_for.clear()
                uuid, token = self.sign_up(keep=True)
                sync_uuid = self.record(token)["sync_uuid"]
                self.fake.encrypted[sync_uuid] = "NEWENCRYPTED"
                main = self.fake.configs[uuid]
                if state == "cannot_rebuild":
                    main["config"]["services"] = []
                elif state == "password_refused":
                    main["password"] = "changed-by-them"
                else:                                   # a change the rebuild follows, whose save fails
                    main["config"]["presets"].insert(0, dict(COMET))
                    if state == "config_refused":
                        self.fake.refusal = lambda config: "Invalid preset options"
                    else:
                        self.fake.refuse_save_for.add(sync_uuid)
                self.changed.clear()
                self.assertEqual(self.checker().check(token), state)
                self.assertEqual(self.record(token)["sync_link"],
                                 f"{INSTANCE}/stremio/{sync_uuid}/NEWENCRYPTED/manifest.json")
                self.assertEqual(self.changed, [token])       # the person's worker starts again with the new link

    def test_a_check_that_fails_is_tried_again_a_period_later_not_at_each_request(self):
        # A check that raised left its due time in the past, so each of the person's requests started another
        # full check of their AIOStreams
        from subsync import recheck
        uuid, token = self.sign_up(keep=True)
        checker = self.checker()
        self.clock.t += 24 * HOUR
        with mock.patch.object(recheck, "rebuild", side_effect=KeyError("a config of an odd shape")), \
                self.assertLogs("subsync.recheck", "WARNING") as logged:
            self.assertTrue(checker.maybe_start(token))
            checker.join(5)
            before = len(self.fake.requests)
            for _ in range(3):
                self.assertFalse(checker.maybe_start(token))
                checker.join(5)
        self.assertEqual(logged.output, ["WARNING:subsync.recheck:a check failed: KeyError"])
        self.assertEqual(len(self.fake.requests), before)
        self.clock.t += 24 * HOUR
        self.assertTrue(checker.maybe_start(token))                    # a period later: checked again
        checker.join(5)
        self.assertEqual(self.record(token)["checked_at"], self.clock.t)

    def test_the_kept_password_is_never_in_clear_on_disk_or_in_the_log(self):
        uuid, token = self.sign_up(keep=True)
        self.checker().check(token)
        self.users.close()
        data = files_bytes(self.dir, "users.sqlite3")
        from subsync.users import Users
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes(), clock=self.clock)
        self.assertNotIn(PASSWORD.encode(), data)
        self.assertFalse([line for line in self.logs.lines if PASSWORD in line])

    def test_one_check_at_a_time_per_aiostreams_however_it_is_spelled(self):
        checker = self.checker()
        self.assertIs(checker._instance("https://AIOS.test:443"), checker._instance(INSTANCE))
        self.assertIs(checker._instance("https://aios.test./"), checker._instance(INSTANCE))
        self.assertIsNot(checker._instance("https://aios.test:8443"), checker._instance(INSTANCE))
        self.assertIsNot(checker._instance(OTHER), checker._instance(INSTANCE))


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class RefreshTest(RecheckHarness, unittest.TestCase):
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
