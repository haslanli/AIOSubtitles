"""Every setting and fixed rule has its one explanation, and every explanation matches what the code does."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import explain  # noqa: E402
from subsync.settings import Settings  # noqa: E402

# The package's own folder, wherever the tests run from (in the image they sit apart from the code).
CODE = os.path.dirname(os.path.abspath(explain.__file__))
REGISTER_NOTE = re.compile(r"\b(?:AD\d{1,3}|S4-\d+|P-S4-[A-Z0-9-]+)\b")


def settings_read_by_the_code():
    names = set()
    for name in os.listdir(CODE):
        if name.endswith(".py") and name != "explain.py":
            with open(os.path.join(CODE, name), encoding="utf-8") as f:
                names |= set(re.findall(r"""["'](SUBSYNC_[A-Z0-9_]+)["']""", f.read()))
    return names


APPROVED_LONG = ("USENET",)


class ExplainTest(unittest.TestCase):
    def test_every_setting_the_code_reads_has_one_explanation(self):
        self.assertEqual(set(explain.SETTINGS), settings_read_by_the_code())

    def test_each_setting_is_in_one_group(self):
        names = [name for _, group in explain.GROUPS for name in group]
        self.assertEqual(len(names), len(set(names)))

    def test_every_documented_default_is_the_codes_default(self):
        defaults = vars(Settings({}))
        for name, (default, _) in explain.SETTINGS.items():
            if default is None or name == "SUBSYNC_VAD_MODEL":   # required (no default), or read outside Settings
                continue
            with self.subTest(name=name):
                self.assertEqual(vars(Settings({name: default})), defaults)

    def test_required_settings_have_no_default(self):
        required = {name for name, (default, _) in explain.SETTINGS.items() if default is None}
        self.assertEqual(required, set(Settings({}).missing()))

    def texts(self):
        yield from ((name, text) for name, (_, text) in explain.SETTINGS.items())
        yield from ((name, text) for name, (_, text) in explain.QUESTIONS.items())
        yield from explain.RULES

    # approved as written though longer than 125 words: the Usenet box's explanation
    def test_each_explanation_is_one_short_plain_paragraph(self):
        for name, text in self.texts():
            with self.subTest(name=name):
                self.assertNotIn("\n", text)
                self.assertEqual(text, text.strip())
                self.assertTrue(text.endswith((".", ")")), text[-30:])
                self.assertLessEqual(len(text.split()), 160 if name in dict(explain.RULES) or name in APPROVED_LONG
                                     else 125)
                self.assertIsNone(REGISTER_NOTE.search(text))

    def test_the_read_failure_rule(self):
        # The owner's approved words
        self.assertEqual(dict(explain.RULES)["A file that can't be reached is tried again later"],
                         "A file that can't be reached on its first read is tried once more 10 seconds later; if it "
                         "still fails, it waits 10 minutes, then 1, 6 and 24 hours, and then isn't read again.")
        self.assertIn("a file's first read is then tried once more 10 seconds later, and a failed file is tried again "
                      "10 minutes, 1, 6 and 24 hours later", explain.SETTINGS["SUBSYNC_READ_TIMEOUT_S"][1])

    def test_the_original_entry_and_the_language_check_rules(self):
        # The approved words
        rules = dict(explain.RULES)
        self.assertEqual(rules["A language with no synced copy is listed with its original"],
                         "A language that has subtitles but no synced copy for a file is listed once, as \"<Language> "
                         "(original)\": the subtitle, unchanged, that fit the file best when it was checked, or else the "
                         "likeliest by its release name, and only if its text is in that language. It isn't synced; a "
                         "synced copy, when there is one, is listed instead.")
        self.assertTrue(rules["Which subtitles are tried first"].endswith(
            " A subtitle whose text isn't in the language it is listed under (judged by its letters and common words "
            "once it has about 300 words) is never synced, offered or served as that language."))
        names = [name for name, _ in explain.RULES]
        self.assertEqual(names.index("A language with no synced copy is listed with its original"),
                         names.index("A copy is offered only after it passes the check") + 1)

    def test_the_edition_order_and_usenet_files_in_the_audio_route(self):
        # The edition sentence (a draft for the owner); the audio route reads Usenet files too
        rules = dict(explain.RULES)
        self.assertIn(" A subtitle that names the same edition as the file (Director's Cut, Extended, Theatrical, …) is "
                      "tried before others of its kind; one that names another edition is tried last. ",
                      rules["Which subtitles are tried first"])
        self.assertIn("(the file's index and its subtitle track, usually a few MB; with the audio route, short parts of "
                      "the sound of a file without a track)", rules["Usenet: only parts of a file, only when you turn it on"])
        self.assertIn("reads the few MB it needs (more for audio syncs).", explain.QUESTIONS["USENET"][1])
        self.assertIn("the most it reads of one file, through your debrid service or Usenet:",
                      explain.QUESTIONS["AUDIO_BUDGET"][1])
        self.assertNotIn("Usenet, never", rules["Public mode: files without a subtitle track"])
        self.assertIn("through your own debrid service or Usenet", rules["Public mode: files without a subtitle track"])
        self.assertIn("(SUBSYNC_AUDIO_AT_ONCE)", rules["Public mode: files without a subtitle track"])

    def test_the_real_debrid_rule_gives_the_reason_the_setup_stops_with(self):
        rules = dict(explain.RULES)
        self.assertIn("Remote Traffic", rules["Real-Debrid is never used"])
        self.assertIn("Usenet: only parts of a file, only when you turn it on", rules)


# The limits' defaults and their texts as the owner approved them
APPROVED = {
    "SUBSYNC_USER_FILES_PER_DAY":
        "New files read per person per day (UTC). Files already in the shared store don't count, and neither does "
        "serving a copy. Past it, that person gets only copies the store already has until the next day; nobody else "
        "is affected. 200 is more than anyone watches in a day, with room for the files apps open without playing "
        "them; it keeps one person, or a leaked link, from using up the service's daily reads.",
    "SUBSYNC_USER_DAILY_MB":
        # with the audio route's sentence; 10,000 and 32 MB
        "Megabytes read per person per day (UTC) through their own sync config. Reading a file's subtitle track takes "
        "about 1 MB, a few MB when the track's lines are pictures; 10,000 is 200 files at the most one file's index "
        "may take (32 MB) with room to spare, so those reads never stop someone before the file limit. A file the "
        "audio route syncs from its sound takes about 60–350 MB, so 10,000 MB covers at least 28 a day; such a sync "
        "starts only when what is left of the day likely covers it, so it rarely stops half-way; if it does, the file "
        "waits for another pick. Past the limit, that person gets only copies already in the store until the next day.",
    "SUBSYNC_MAX_LANGUAGES":
        "How many languages each person may choose, from 1 to 4. Each language is a separate sync for every file they "
        "play: its own subtitle downloads and a few seconds of work, while the file itself is read once. Recommended: "
        "4, the most the pages offer; lower it only when the server can't keep up. A lower limit applies when people "
        "next save their languages; until then they keep the ones they have.",
    "SUBSYNC_ACTIVE_WORKERS":
        "How many people can have a worker (the part that finds and syncs their files) running at once. A worker with "
        "nothing to do stops after a while (30 minutes by default). Past the limit, a person's requests get only "
        "copies already in the shared store until a worker frees up. Each worker takes about 2.5 MB of memory, so 100 "
        "take about 250 MB; keep the limit within what the server's memory allows.",
    "SUBSYNC_MAX_CONNECTIONS":
        "Requests handled at once. Each one takes a thread and some memory, so past it the service answers \"busy, "
        "try again\" (HTTP 503) at once instead of running out of memory. Apps ask for subtitles again on their own. "
        "256 leaves room for 200 people's apps asking at once; raise it together with the memory the service may use.",
    "SUBSYNC_SYNCS_AT_ONCE":
        "How many files the service works on at once for everyone, or auto for one per processor it may use. "
        "Recommended: auto, since one sync keeps about one processor busy. More lets people who play new files at "
        "the same time wait less, at more processor use and more requests to each AIOStreams at once; one public "
        "AIOStreams shared by many people limits how many requests one address may make, so give a number when the "
        "processors are many and that AIOStreams is shared. A torrent's file is never read twice at once: whoever "
        "plays it second waits for the first one's work and reads nothing.",
    "SUBSYNC_SECRET_KEY":
        "The service's secret, at least 32 random bytes written as base64url; setup makes one and keeps it in "
        "state/subsync.env, readable only by your user (by hand: what python3 -c \"import secrets; "
        "print(secrets.token_urlsafe(32))\" prints). It encrypts each person's record together with their personal "
        "link, so neither the database nor this secret alone opens a record. Required in public mode; never logged or "
        "shown. Keep that file in your backups: if the secret is lost or changed, everyone has to sign up again.",
}


class LimitsTest(unittest.TestCase):
    def test_the_ad446_defaults(self):
        from subsync.settings import available_cpus
        s = Settings({})
        self.assertEqual(s.syncs_at_once, available_cpus())
        self.assertEqual(s.active_workers, 100)
        self.assertEqual(s.max_connections, 256)
        self.assertEqual(s.user_files_per_day, 200)                  #
        self.assertEqual(s.user_daily_bytes, 10000 * 1024 * 1024)
        self.assertEqual(s.daily_budget, int(100e9))                 # 100 GB a day
        self.assertEqual(s.max_users, 200)

    def test_the_documented_defaults_are_the_ad446_ones(self):
        documented = {name: explain.SETTINGS[name][0] for name in (
            "SUBSYNC_SYNCS_AT_ONCE", "SUBSYNC_ACTIVE_WORKERS", "SUBSYNC_MAX_CONNECTIONS", "SUBSYNC_USER_FILES_PER_DAY",
            "SUBSYNC_USER_DAILY_MB", "SUBSYNC_DAILY_BUDGET_GB")}
        self.assertEqual(documented, {"SUBSYNC_SYNCS_AT_ONCE": "auto", "SUBSYNC_ACTIVE_WORKERS": "100",
                                      "SUBSYNC_MAX_CONNECTIONS": "256", "SUBSYNC_USER_FILES_PER_DAY": "200",
                                      "SUBSYNC_USER_DAILY_MB": "10000", "SUBSYNC_DAILY_BUDGET_GB": "100"})

    def test_the_approved_limit_texts(self):
        for name, text in APPROVED.items():
            with self.subTest(name=name):
                self.assertEqual(explain.SETTINGS[name][1], text)

    def test_no_explanation_says_public_mode_only(self):
        for name, (_, text) in explain.SETTINGS.items():
            with self.subTest(name=name):
                self.assertNotIn("Public mode only", text)
                self.assertTrue(text[0].isupper() or text[0].isdigit(), text[:30])

    def test_no_text_names_an_old_default(self):
        texts = [text for _, text in explain.SETTINGS.values()] + [text for _, text in explain.RULES]
        for text in texts:
            for old in ("40 is several days", "so 2 suits", "40 files and 500 MB", "60 files and 1,000 MB",
                        "60 is a long day", "1,000 is 60 files", "(16 MB)", "Recommended: 16.", "50 is far above"):
                self.assertNotIn(old, text)
        self.assertIn("200 files and 10,000 MB", dict(explain.RULES)["Public mode: limits per person"])

    def test_the_index_cap_text(self):
        # 32 MB, and a file whose index is over the cap isn't read again until the cap is raised
        default, text = explain.SETTINGS["SUBSYNC_INDEX_CAP_MB"]
        self.assertEqual(default, "32")
        self.assertIn("Recommended: 32.", text)
        self.assertIn("A file whose index is larger keeps its original subtitles and isn't read again until the cap is "
                      "raised.", text)
        self.assertIn("100 is far above normal use", explain.SETTINGS["SUBSYNC_DAILY_BUDGET_GB"][1])

    def test_public_mode_says_setup_turns_it_on(self):
        text = explain.SETTINGS["SUBSYNC_PUBLIC"][1]
        self.assertIn("Leave it off for your own install; setup turns it on when you host a public instance.", text)
        self.assertNotIn("./setup doesn't use it", text)


if __name__ == "__main__":
    unittest.main()
