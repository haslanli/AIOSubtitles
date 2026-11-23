"""A self-hosted install's settings written by hand (state/subsync.env): every number read one way, with the range its
explanation implies, every on/off setting on or off, the languages and the addon
key. A value that can't work is one plain line at the start, which then stops, never a traceback or a service
that fails far from the cause."""
import dataclasses
import hashlib
import json
import os
import re
import unittest
from unittest import mock

import helpers  # noqa: F401  (first: it puts the package on the import path)
from subsync import ALGO_VERSION, RULE_REVISION, explain, settings as settings_module  # noqa: E402
from subsync.settings import GB, MB, Rules, Settings  # noqa: E402

PUBLIC = {"SUBSYNC_PUBLIC": "on"}


def settings_source():
    with open(settings_module.__file__, encoding="utf-8") as f:
        return f.read()


def number_settings():
    """{name: "whole" or "number"} of every setting Settings reads as a number through its one reader."""
    return {name: kind for kind, name in re.findall(r'read\.(whole|number)\("(SUBSYNC_[A-Z0-9_]+)"', settings_source())}


class NumbersTest(unittest.TestCase):
    def test_a_number_written_wrong_is_one_plain_line_naming_its_setting(self):
        # Empty, a decimal where a whole number is needed, an exponent, nan or inf raised out of Settings, so
        # the service stopped with a traceback and restarted in a loop; the owner's lines, word for word
        for name, value, line in (
                ("SUBSYNC_MAX_TRIES", "6.0", "SUBSYNC_MAX_TRIES must be a whole number from 1, like 6."),
                ("SUBSYNC_MAX_TRIES", "", "SUBSYNC_MAX_TRIES must be a whole number from 1, like 6."),
                ("SUBSYNC_USENET_LISTINGS_PER_DAY", "1e3",
                 "SUBSYNC_USENET_LISTINGS_PER_DAY must be a whole number from 0, like 10."),
                ("SUBSYNC_PORT", "", "SUBSYNC_PORT must be a whole number from 1 to 65535."),
                ("SUBSYNC_READ_TIMEOUT_S", "nan", "SUBSYNC_READ_TIMEOUT_S must be a number above 0, like 60."),
                ("SUBSYNC_DAILY_BUDGET_GB", "inf", "SUBSYNC_DAILY_BUDGET_GB must be a number from 0, like 100."),
                ("SUBSYNC_STORE_CAP_MB", "lots", "SUBSYNC_STORE_CAP_MB must be a number from 0, like 2048."),
                ("SUBSYNC_403_BACKOFF_S", "1e3", "SUBSYNC_403_BACKOFF_S must be a whole number from 1, like 900."),
                ("SUBSYNC_BACKUP_DAYS", "a month", "SUBSYNC_BACKUP_DAYS must be a whole number from 0.")):
            with self.subTest(name=name, value=value):
                s = Settings({name: value})
                self.assertEqual(s.conflicts(), [line])
                self.assertEqual(s.written_wrong, [line])

    def test_a_value_its_explanation_rules_out_is_refused(self):
        # An impossible value started the service and failed far from the cause (every read failed with a
        # 0 s timeout, every file stopped on a day's budget of -1 GB); a size whose bytes no number holds crashed it
        for name, value, line in (
                ("SUBSYNC_PORT", "99999", "SUBSYNC_PORT must be a whole number from 1 to 65535."),
                ("SUBSYNC_PORT", "0", "SUBSYNC_PORT must be a whole number from 1 to 65535."),
                ("SUBSYNC_READ_TIMEOUT_S", "0", "SUBSYNC_READ_TIMEOUT_S must be a number above 0, like 60."),
                ("SUBSYNC_PICK_WAIT_MS", "-5", "SUBSYNC_PICK_WAIT_MS must be a number above 0, like 14000."),
                ("SUBSYNC_WORKER_IDLE_MIN", "0", "SUBSYNC_WORKER_IDLE_MIN must be a number above 0, like 30."),
                ("SUBSYNC_DAILY_BUDGET_GB", "-1", "SUBSYNC_DAILY_BUDGET_GB must be a number from 0, like 100."),
                ("SUBSYNC_FILE_CAP_GB", "1e300", "SUBSYNC_FILE_CAP_GB must be a number from 0, like 6."),
                ("SUBSYNC_MAX_USERS", "-5", "SUBSYNC_MAX_USERS must be a whole number from 0, like 200."),
                ("SUBSYNC_MAIN_ALTERNATIVES", "0", "SUBSYNC_MAIN_ALTERNATIVES must be a whole number from 1, like 3."),
                ("SUBSYNC_MAX_CONNECTIONS", "0", "SUBSYNC_MAX_CONNECTIONS must be a whole number from 1, like 256.")):
            with self.subTest(name=name, value=value):
                self.assertEqual(Settings({name: value}).conflicts(), [line])

    def test_a_keep_days_below_1_stops_the_start(self):
        # 0 (which may be read as "keep for ever") deleted every record not used today at the first
        # housekeeping, -1 every record; the owner's start line, word for word
        from subsync import __main__ as entry
        line = "SUBSYNC_USER_KEEP_DAYS must be a whole number from 1, like 180."
        for value in ("0", "-1", "1.5", "a year", ""):
            with self.subTest(value=value):
                self.assertEqual(Settings(dict(PUBLIC, SUBSYNC_USER_KEEP_DAYS=value)).conflicts(), [line])
        self.assertEqual((Settings({"SUBSYNC_USER_KEEP_DAYS": "1"}).user_keep_days,
                          Settings({"SUBSYNC_USER_KEEP_DAYS": "1"}).conflicts()), (1, []))
        env = {"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": helpers.KEY, "SUBSYNC_PUBLIC_BASE": "https://subs.test",
               "SUBSYNC_USER_KEEP_DAYS": "0"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(entry.logging, "basicConfig"), \
                self.assertLogs("subsync", "INFO") as logs, self.assertRaises(SystemExit) as stopped:
            entry.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(logs.output, ["ERROR:subsync:not started: " + line])
        self.assertTrue(explain.SETTINGS["SUBSYNC_USER_KEEP_DAYS"][1].startswith(
            "Days without a single request before a person's record is deleted (a whole number from 1). Their sync "
            "config can't be removed "))

    def test_a_number_in_another_scripts_digits_is_written_wrong(self):
        # Int and float read every script's decimal digits as if they were ASCII ones, so these were read
        # as 6, 8791, 1.5 GB, 1024 MB and 60 s
        for name, value, line in (
                ("SUBSYNC_MAX_TRIES", "٦", "SUBSYNC_MAX_TRIES must be a whole number from 1, like 6."),
                ("SUBSYNC_PORT", "８７９１", "SUBSYNC_PORT must be a whole number from 1 to 65535."),
                ("SUBSYNC_DAILY_BUDGET_GB", "١.٥", "SUBSYNC_DAILY_BUDGET_GB must be a number from 0, like 100."),
                ("SUBSYNC_STORE_CAP_MB", "\uff11\uff10\uff12\uff14",
                 "SUBSYNC_STORE_CAP_MB must be a number from 0, like 2048."),
                ("SUBSYNC_READ_TIMEOUT_S", "６０", "SUBSYNC_READ_TIMEOUT_S must be a number above 0, like 60.")):
            with self.subTest(name=name, value=value):
                s = Settings({name: value})
                self.assertEqual(s.conflicts(), [line])
                self.assertEqual(s.written_wrong, [line])

    def test_every_documented_zero_keeps_its_meaning(self):
        # "0 removes the limit" (the audio and per-file limits), "0 turns Usenet off", "0 turns the check off", "0 stops
        # new audio syncs", "0: the first check decides", nothing read in a day
        zeros = {"SUBSYNC_AUDIO_BUDGET_GB": ("audio_budget", None), "SUBSYNC_FILE_CAP_GB": ("file_cap", None),
                 "SUBSYNC_USENET_LISTINGS_PER_DAY": ("usenet_listings_per_day", 0),
                 "SUBSYNC_RECHECK_HOURS": ("recheck_hours", 0), "SUBSYNC_USER_AUDIO_PER_DAY": ("user_audio_per_day", 0),
                 "SUBSYNC_USER_OPENED_PER_DAY": ("user_opened_per_day", 0),
                 "SUBSYNC_AUDIO_EXTRA_WINDOWS": ("audio_extra", 0), "SUBSYNC_DAILY_BUDGET_GB": ("daily_budget", 0),
                 "SUBSYNC_ACTIVE_WORKERS": ("active_workers", 0), "SUBSYNC_AUDIO_LEAD_MS": ("audio_lead", 0)}
        for name, (attribute, value) in zeros.items():
            with self.subTest(name=name):
                s = Settings({name: "0"})
                self.assertEqual((getattr(s, attribute), s.conflicts()), (value, []))

    def test_every_number_setting_is_read_by_the_one_reader_with_a_default_it_accepts(self):
        names = number_settings()
        self.assertGreaterEqual(len(names), 40)
        self.assertEqual(Settings({}).written_wrong, [])
        self.assertNotRegex(settings_source(), r"(int|float)\(g\(")        # no number parsed beside it
        for name in names:
            with self.subTest(name=name):
                wrong = Settings({name: "x"})
                self.assertEqual(len(wrong.written_wrong), 1)
                self.assertTrue(wrong.written_wrong[0].startswith(f"{name} must be a "))
                # read as its default: the same settings as an install without the line
                self.assertEqual({k: v for k, v in vars(wrong).items() if k != "written_wrong"},
                                 {k: v for k, v in vars(Settings({})).items() if k != "written_wrong"})

    def test_values_that_work_today_still_do(self):
        s = Settings({"SUBSYNC_PORT": " 8791 ", "SUBSYNC_STORE_CAP_MB": "2.5", "SUBSYNC_DAILY_BUDGET_GB": "0.5",
                      "SUBSYNC_PICK_WAIT_MS": "9500", "SUBSYNC_WORKER_IDLE_MIN": "1.5", "SUBSYNC_BACKUP_DAYS": "30",
                      "SUBSYNC_SNAP_WITHIN": "0.9", "SUBSYNC_FILE_CAP_GB": "1e3"})
        self.assertEqual(s.conflicts(), [])
        self.assertEqual((s.port, s.cap_bytes, s.daily_budget, s.pick_wait, s.worker_idle_s, s.backup_days,
                          s.snap_within, s.file_cap),
                         (8791, int(2.5 * MB), int(0.5 * GB), 9.5, 90.0, 30, 0.9, int(1e3 * GB)))
        self.assertEqual(Settings({"SUBSYNC_BACKUP_DAYS": " "}).backup_days, 0)         # empty: not said
        for name, kind in number_settings().items():
            with self.subTest(name=name):
                default = explain.SETTINGS[name][0]
                self.assertEqual(Settings({name: default}).conflicts(), [], default)
                if kind == "whole" and default:          # SUBSYNC_BACKUP_DAYS has none: empty is "not said"
                    self.assertTrue(default.isdecimal(), default)


class SwitchesTest(unittest.TestCase):
    # Any word but on, 1, true or yes was read as off without a word, so SUBSYNC_SKIP_AI=enabled turned that
    # protection off; on or off in any of their words, empty off as before, any other word one line (the owner's)
    def test_an_on_off_setting_written_wrong_is_one_plain_line_naming_it(self):
        for name, value, attribute, default in (("SUBSYNC_SKIP_AI", "enabled", "skip_ai", True),
                                                ("SUBSYNC_FAST_PATH", "y", "fast_path", True),
                                                ("SUBSYNC_SNAP", "ture", "snap", True),
                                                ("SUBSYNC_PENDING_ENTRY", "enabled", "pending_entry", False),
                                                ("SUBSYNC_PUBLIC", "enabled", "public", False)):
            with self.subTest(name=name, value=value):
                s = Settings({name: value})
                self.assertEqual(s.conflicts(), [f"{name} must be on or off."])
                self.assertEqual(s.written_wrong, [f"{name} must be on or off."])
                self.assertIs(getattr(s, attribute), default)      # read as its default, the start refused

    def test_on_and_off_in_each_of_their_words_and_empty_still_work(self):
        for value, on in (("on", True), (" ON ", True), ("1", True), ("true", True), ("Yes", True), ("off", False),
                          ("Off", False), ("0", False), ("false", False), ("NO", False), ("", False), (" ", False)):
            with self.subTest(value=value):
                s = Settings({"SUBSYNC_SKIP_AI": value, "SUBSYNC_AUDIO_ROUTE": value})
                self.assertEqual((s.skip_ai, s.audio_route, s.conflicts()), (on, on, []))

    def test_every_on_off_setting_is_read_by_the_one_reader(self):
        switches = re.findall(r'read\.switch\("(SUBSYNC_[A-Z0-9_]+)"\)', settings_source())
        # every setting documented as on or off, but SUBSYNC_AUDIO_ON_PICK: the old setting's reading stays (below)
        documented = {name for name, (default, _) in explain.SETTINGS.items() if default in ("on", "off")}
        self.assertEqual(set(switches), documented - {"SUBSYNC_AUDIO_ON_PICK"})
        self.assertEqual(len(switches), len(set(switches)))
        self.assertNotRegex(settings_source(), r"_on\(g\(")             # no switch read beside it
        for name in switches:
            with self.subTest(name=name):
                wrong = Settings({name: "x"})
                self.assertEqual(wrong.written_wrong, [f"{name} must be on or off."])
                self.assertEqual({k: v for k, v in vars(wrong).items() if k != "written_wrong"},
                                 {k: v for k, v in vars(Settings({})).items() if k != "written_wrong"})

    def test_the_old_audio_on_pick_setting_is_read_as_before(self):
        # documented and kept: anything but on means open
        for value, when in (("on", "pick"), ("pick", "open"), ("", "open")):
            with self.subTest(value=value):
                s = Settings({"SUBSYNC_AUDIO_ON_PICK": value})
                self.assertEqual((s.audio_when, s.conflicts()), (when, []))

    def test_the_start_says_a_misspelt_switch_before_what_must_be_set(self):
        # read as off, a misspelt SUBSYNC_PUBLIC made a public instance ask for a self-hosted install's settings
        from subsync import __main__ as entry
        with mock.patch.dict(os.environ, {"SUBSYNC_PUBLIC": "enabled"}, clear=True), \
                mock.patch.object(entry.logging, "basicConfig"), self.assertLogs("subsync", "INFO") as logs, \
                self.assertRaises(SystemExit) as stopped:
            entry.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(logs.output, ["ERROR:subsync:not started: SUBSYNC_PUBLIC must be on or off."])

    def test_the_start_says_every_setting_written_wrong_and_missing_at_once(self):
        # A missing setting hid a conflicting one until the next start
        from subsync import __main__ as entry
        for env, lines in (
                ({"SUBSYNC_AUDIO_WHEN": "later"},
                 ["not started: SUBSYNC_AUDIO_WHEN must be pick or open.",
                  "not started: set SUBSYNC_SOURCE_MANIFEST, SUBSYNC_SOURCE_PASSWORD, SUBSYNC_PUBLIC_BASE (./setup "
                  "writes them)"]),
                ({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_AT_ONCE": "x"},
                 ["not started: SUBSYNC_AUDIO_AT_ONCE must be a whole number from 1, or auto.",
                  "not started: set SUBSYNC_SECRET_KEY, SUBSYNC_PUBLIC_BASE"])):
            with self.subTest(env=env):
                with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(entry.logging, "basicConfig"), \
                        self.assertLogs("subsync", "INFO") as logs, self.assertRaises(SystemExit) as stopped:
                    entry.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertEqual(logs.output, ["ERROR:subsync:" + line for line in lines])


class LanguagesTest(unittest.TestCase):
    def test_repeats_are_dropped_quietly(self):
        # En,eng gave English twice, its pick entry twice in the app and needless work on every request
        s = Settings({"SUBSYNC_LANGUAGES": "en,eng, English ,rus,ru"})
        self.assertEqual((s.languages, s.conflicts()), (["eng", "rus"], []))
        self.assertEqual(Settings({"SUBSYNC_FALLBACK_REFERENCE": "eng,en"}).fallback_reference, ("eng",))

    def test_an_unknown_code_or_an_empty_list_stops_a_self_hosted_start(self):
        lead = "SUBSYNC_LANGUAGES must be language codes separated by commas, like eng,rus: "
        self.assertEqual(Settings({"SUBSYNC_LANGUAGES": "eng,xx"}).conflicts(), [lead + "xx isn't one."])
        self.assertEqual(Settings({"SUBSYNC_LANGUAGES": " xx , Klingon"}).conflicts(),
                         [lead + "xx isn't one.", lead + "Klingon isn't one."])
        for empty in ("", " , "):
            with self.subTest(value=empty):
                self.assertEqual(Settings({"SUBSYNC_LANGUAGES": empty}).conflicts(),
                                 ["SUBSYNC_LANGUAGES must name at least one language, like eng."])

    def test_a_public_instance_uses_each_persons_own_languages(self):
        for value in ("", "xx"):
            with self.subTest(value=value):
                self.assertEqual(Settings(dict(PUBLIC, SUBSYNC_LANGUAGES=value)).conflicts(), [])


class RulesTest(unittest.TestCase):
    """The settings that change a verdict are one Rules, passed whole to the pipeline and hashed whole for the
    stored verdicts' rule (rule_id), so a setting the pipeline reads can't be left out of the rule id."""

    def test_the_rules_are_the_settings_of_the_same_names(self):
        s = Settings({"SUBSYNC_SNAP_WITHIN": "0.97", "SUBSYNC_FALLBACK_REFERENCE": "pt,en", "SUBSYNC_BETTER": "off",
                      "SUBSYNC_AUDIO_LEAD_MS": "150"})
        r = s.rules
        for f in dataclasses.fields(Rules):
            self.assertEqual(getattr(r, f.name), getattr(s, f.name), f.name)
        self.assertEqual((r.snap_within, r.fallback_reference, r.better, r.audio_lead),
                         (0.97, ("por", "eng"), None, 0.15))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.snap = False

    def test_each_rule_changes_the_rule_id(self):
        base = Settings({}).rule_id
        changed = {"snap": False, "snap_within": 0.9, "gate_splits": False, "image_ref": False, "better": None,
                   "audio_better": None, "audio_lead": 0.0, "audio_budget": None, "audio_extra": 0,
                   "prefer_plain": False, "fallback_reference": ("por",), "text_pairing": False, "sections": False,
                   "shift_path": False}
        self.assertEqual(set(changed), {f.name for f in dataclasses.fields(Rules)})
        for name, value in changed.items():
            s = Settings({})
            setattr(s, name, value)
            self.assertNotEqual(s.rule_id, base, name)

    def test_the_rule_ids_of_stored_verdicts_are_kept(self):
        # the same keys and values hashed as when rule_id listed the settings by hand: no stored verdict is judged
        # again for the change
        for env in ({}, {"SUBSYNC_BETTER": "off", "SUBSYNC_AUDIO_BUDGET_GB": "0", "SUBSYNC_FALLBACK_REFERENCE": "pt"}):
            s = Settings(env)
            listed = {"algo": ALGO_VERSION, "revision": RULE_REVISION, "better": s.better,
                      "audio_better": s.audio_better, "snap": s.snap, "snap_within": s.snap_within,
                      "gate_splits": s.gate_splits, "image_ref": s.image_ref, "audio_lead": s.audio_lead,
                      "audio_budget": s.audio_budget,
                      "prefer_plain": s.prefer_plain, "shift_path": s.shift_path,
                      "fallback_reference": s.fallback_reference, "text_pairing": s.text_pairing,
                      "sections": s.sections, "audio_extra": s.audio_extra}
            self.assertEqual(s.rule_id, hashlib.sha256(json.dumps(listed, sort_keys=True).encode()).hexdigest()[:12])


class AddonKeyTest(unittest.TestCase):
    def test_a_key_with_a_slash_stops_a_self_hosted_start(self):
        # It can't be one part of an address, so every keyed route answered "not found" for good
        self.assertEqual(Settings({"SUBSYNC_ADDON_KEY": "my/key"}).conflicts(), ["SUBSYNC_ADDON_KEY can't contain /."])
        for key in ("/k3y/", "my key", "clé", "ab_DE-12", ""):
            with self.subTest(key=key):
                self.assertEqual(Settings({"SUBSYNC_ADDON_KEY": key}).conflicts(), [])
        self.assertEqual(Settings(dict(PUBLIC, SUBSYNC_ADDON_KEY="my/key")).conflicts(), [])    # not used there


if __name__ == "__main__":
    unittest.main()
