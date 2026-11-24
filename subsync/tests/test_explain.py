"""Every setting and fixed rule has its one explanation, and every explanation matches what the code does."""
import os
import re
import unittest

import helpers  # noqa: F401  (first: it puts the package on the import path)
from helpers import HAVE_CRYPTOGRAPHY, NEEDS
from subsync import explain
from subsync.settings import Settings

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


# approved as written though longer than 125 words (a setting or question) or 160 (a fixed rule), each with its approved
# word count, which is its own cap: the Usenet box's explanation, the audio limit's and the word check's with the
# sentences added to them, the daily data limit's and the rule on what is kept with
# its deletion sentence and the 7 days of each person's original subtitles, and the
# workers' memory sizing with what re-timing takes
APPROVED_LONG = {
    "USENET": 140,
    "SUBSYNC_AUDIO_BUDGET_GB": 133,
    "SUBSYNC_TEXT_PAIRING": 143,
    "SUBSYNC_USER_DAILY_MB": 144,
    "SUBSYNC_ACTIVE_WORKERS": 137,
    "Public mode: what is kept, and for how long": 215,
}


def word_cap(name):
    """The most words a text may have: its approved count when it was approved as long, else 160 for a fixed rule and
    125 for a setting or question."""
    if name in APPROVED_LONG:
        return APPROVED_LONG[name]
    return 160 if name in dict(explain.RULES) else 125


class ExplainTest(unittest.TestCase):
    def test_every_setting_the_code_reads_has_one_explanation(self):
        self.assertEqual(set(explain.SETTINGS), settings_read_by_the_code())

    def test_each_setting_is_in_one_group(self):
        names = [name for _, group in explain.GROUPS for name in group]
        self.assertEqual(len(names), len(set(names)))

    def test_every_documented_default_is_the_codes_default(self):
        defaults = vars(Settings({}))
        for name, (default, _) in explain.SETTINGS.items():
            if default is None:   # required: no default
                continue
            with self.subTest(name=name):
                self.assertEqual(vars(Settings({name: default})), defaults)

    def test_settings_take_their_defaults_from_here(self):
        # Each default is written once, here, and Settings reads it (no second literal in settings.py)
        from unittest import mock
        cases = (("SUBSYNC_MAX_TRIES", "7", lambda s: s.max_tries, 7),
                 ("SUBSYNC_BETTER_PAIRED", "0.9", lambda s: s.better["paired"], 0.9),
                 ("SUBSYNC_AUDIO_BETTER_MATCHED", "30", lambda s: s.audio_better["matched"], 30),
                 ("SUBSYNC_AUDIO_ON_PICK", "off", lambda s: s.audio_when, "open"))
        for name, value, read, expected in cases:
            with self.subTest(name=name), \
                    mock.patch.dict(explain.SETTINGS, {name: (value, explain.SETTINGS[name][1])}):
                self.assertEqual(read(Settings({})), expected)

    def test_required_settings_have_no_default(self):
        required = {name for name, (default, _) in explain.SETTINGS.items() if default is None}
        self.assertEqual(required, set(Settings({}).missing()))

    def texts(self):
        yield from ((name, text) for name, (_, text) in explain.SETTINGS.items())
        yield from ((name, text) for name, (_, text) in explain.QUESTIONS.items())
        yield from explain.RULES

    def test_each_explanation_is_one_short_plain_paragraph(self):
        for name, text in self.texts():
            with self.subTest(name=name):
                self.assertNotIn("\n", text)
                self.assertEqual(text, text.strip())
                self.assertTrue(text.endswith((".", ")")), text[-30:])
                self.assertLessEqual(len(text.split()), word_cap(name))
                self.assertIsNone(REGISTER_NOTE.search(text))

    def test_a_text_approved_as_long_may_not_exceed_its_own_count(self):
        # A text approved as long was held to 160 words, so the Usenet box's (140) could still grow by 20; now
        # its approved count is its cap, and every other text keeps 125 (a setting or question) or 160 (a fixed rule)
        texts = {}
        for name, text in self.texts():
            texts.setdefault(name, []).append(text)
        self.assertEqual(word_cap("USENET"), 140)
        self.assertEqual(word_cap("Public mode: what is kept, and for how long"), 215)     # 171 before
        self.assertEqual(word_cap("SUBSYNC_AUDIO_AT_ONCE"), 125)
        self.assertEqual(word_cap("AUDIO"), 125)
        self.assertEqual(word_cap("Real-Debrid is never used"), 160)
        for name, cap in APPROVED_LONG.items():
            with self.subTest(name=name):
                self.assertEqual(len(texts.get(name, [])), 1)              # one text of that name, which is there
                self.assertGreater(cap, 160 if name in dict(explain.RULES) else 125)   # only texts past the usual limit
                self.assertEqual(word_cap(name), cap)
                count = len(texts[name][0].split())
                self.assertLessEqual(count, cap)
                self.assertEqual(count, cap)          # the approved text itself: one word more fails the check above

    def test_the_read_failure_rule(self):
        # The owner's approved words, with a Usenet file's waits
        self.assertEqual(dict(explain.RULES)["A file that can't be reached is tried again later"],
                         "A file that can't be reached on its first read is tried once more 10 seconds later; if it "
                         "still fails, it waits 10 minutes, then 1, 6 and 24 hours, and then isn't read again. A "
                         "Usenet file gets no second try: nothing more is looked for on Usenet until 00:00 UTC, and "
                         "the file waits 1, 6 and 24 hours.")
        self.assertIn("A file's first read is then tried once more 10 seconds later, and a failed file is tried again "
                      "10 minutes, 1, 6 and 24 hours later (a Usenet file: no second try, then 1, 6 and 24 hours).",
                      explain.SETTINGS["SUBSYNC_READ_TIMEOUT_S"][1])

    def test_the_password_rule_names_the_box_that_adds_aiosubtitles(self):
        # "if you leave the box checked" read as the keep-password box, named just before it; and two box
        # explanations said "Unchecked" where the others say "Ticked" and "Unticked"
        rule = dict(explain.RULES)["Public mode: your AIOStreams password is used once, unless you choose to keep it"]
        self.assertIn("; with Usenet on, a second one for Usenet only) and, if you leave \"Add AIOSubtitles to my "
                      "AIOStreams config\" ticked, to add AIOSubtitles to your own config. Without that separate "
                      "config,", rule)
        self.assertNotIn("the box checked", rule)
        self.assertIn(" Recommended. Unticked, nothing in your own config changes and you add the link yourself as a "
                      "custom addon.", explain.PUBLIC_FIELDS["add_addon"])
        self.assertIn(" until it is back. Unticked, your config keeps them and nothing is removed. ",
                      explain.PUBLIC_FIELDS["clean_up"])
        for text in [text for _, text in explain.RULES] + list(explain.PUBLIC_FIELDS.values()):
            self.assertNotIn("nchecked", text)
            self.assertNotIn("box checked", text)

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

    def test_the_snapped_bar_counts_the_lines_that_pair(self):
        # it said a share of all lines; the check counts the paired ones
        self.assertTrue(explain.SETTINGS["SUBSYNC_SNAP_WITHIN"][1].startswith(
            "The share of a snapped copy's paired lines that must start within 200 ms of the file's track "
            "(0.95 = 95 %). "))

    def test_the_sync_configs_texts_name_the_usenet_only_install(self):
        # "Required: subsync doesn't start without it", yet with Real-Debrid as the only debrid service and Usenet on,
        # ./setup leaves the sync config out and subsync lists every file through the Usenet config, which
        # then holds the subtitle sources too (aiosconfig.usenet_sync_config's `subtitles`)
        self.assertIn(" Required on your own install, except where Real-Debrid is your only debrid service and Usenet "
                      "is on: then ./setup makes no such config and leaves this empty. ",
                      explain.SETTINGS["SUBSYNC_SOURCE_MANIFEST"][1])
        self.assertTrue(explain.SETTINGS["SUBSYNC_SOURCE_PASSWORD"][1].endswith(
            " Required whenever SUBSYNC_SOURCE_MANIFEST is. Keep it private."))
        self.assertIn(" subsync lists through it only when your debrid service doesn't have the file you play, and for "
                      "every file when Real-Debrid is your only debrid service; then it also holds your subtitle "
                      "sources. Keep it private.", explain.SETTINGS["SUBSYNC_USENET_MANIFEST"][1])
        for name in ("SUBSYNC_SOURCE_MANIFEST", "SUBSYNC_SOURCE_PASSWORD"):
            with self.subTest(name=name):
                self.assertNotIn("doesn't start without it", explain.SETTINGS[name][1])
        usenet_only = {"SUBSYNC_USENET": "on", "SUBSYNC_USENET_MANIFEST": "u", "SUBSYNC_USENET_PASSWORD": "p",
                       "SUBSYNC_PUBLIC_BASE": "https://x.test"}
        self.assertEqual(Settings(usenet_only).missing(), [])
        self.assertEqual(Settings(dict(usenet_only, SUBSYNC_USENET="off")).missing(),
                         ["SUBSYNC_SOURCE_MANIFEST", "SUBSYNC_SOURCE_PASSWORD"])

    def test_the_check_rule_names_its_setting(self):
        # a fixed rule stated SUBSYNC_RECHECK_HOURS' default as fixed: "checked about once a day"
        rules = dict(explain.RULES)
        self.assertNotIn("Public mode: your AIOStreams is checked about once a day", rules)
        self.assertTrue(rules["Public mode: your AIOStreams is checked while you use it"].startswith(
            "While you use AIOSubtitles, it checks your AIOStreams in the background, at most once a day by default "
            "(SUBSYNC_RECHECK_HOURS; 0 turns the check off): whether your config still exists "))
        self.assertEqual(explain.SETTINGS["SUBSYNC_RECHECK_HOURS"][0], "24")
        self.assertEqual(Settings({"SUBSYNC_RECHECK_HOURS": "0"}).recheck_hours, 0)

    def test_the_kept_password_and_the_records_contents(self):
        # the rule said "Your AIOStreams password is never kept", though "Keep my password" keeps it in the record, and
        # it left out what else the record holds
        rules = dict(explain.RULES)
        self.assertNotIn("Public mode: your AIOStreams password is used once", rules)
        self.assertIn("Public mode: your AIOStreams password is used once, unless you choose to keep it", rules)
        self.assertIn(": the address of their AIOStreams, the id of their config, AIOSubtitles' own config there and "
                      "its password (with Usenet on, its Usenet config's too), their languages and their answer for "
                      "files with no subtitles of their own, their own TMDB key if they added one, and when their "
                      "AIOStreams was last checked, plus how much was read for them each day. For up to 7 days after "
                      "someone plays a file, the original subtitles listed for it in their languages are kept too "
                      "(and, while it waits for their pick, its title, name and size), under a key that only their "
                      "personal link leads to. Your AIOStreams password is kept only if you tick \"Keep my "
                      "password\", in the same encrypted record, until you untick it. ",
                      rules["Public mode: what is kept, and for how long"])

    def test_no_text_says_the_password_is_never_kept(self):
        # Every text of this module, the README's rules and the public pages' texts alike
        never = re.compile(r"password (?:is|was) never (?:kept|stored|saved)|never (?:keeps|stores|saves) (?:your|the|"
                           r"their) (?:AIOStreams )?password", re.IGNORECASE)

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    yield from strings(item)

        texts = [text for name, value in vars(explain).items() if name.isupper() for text in strings(value)]
        self.assertIn("Keep my password", " ".join(texts))
        for text in texts:
            with self.subTest(text=text[:60]):
                self.assertIsNone(never.search(text))

    def test_the_public_settings_that_stop_the_service_say_so(self):
        # Settings.conflicts refuses these in public mode; the texts called the TMDB pair only "not recommended" and
        # said nothing of Wyzie. The setup question keeps its own words.
        self.assertIn(" Off: the service's own key, if any, is used for everyone; with SUBSYNC_TMDB_KEY set it must "
                      "stay off, or the service doesn't start. ", explain.SETTINGS["SUBSYNC_TMDB_PERSONAL"][1])
        self.assertNotIn("recommended if you set a key", explain.SETTINGS["SUBSYNC_TMDB_PERSONAL"][1])
        self.assertEqual(explain.SETTINGS["SUBSYNC_WYZIE_KEY"][1],
                         explain.QUESTIONS["WYZIE_KEY"][1] + " A public instance doesn't start with it set.")
        self.assertTrue(explain.QUESTIONS["WYZIE_KEY"][1].endswith(
            " Kept only on this machine, in state/, readable only by your user."))
        public = {"SUBSYNC_PUBLIC": "on"}
        self.assertTrue(Settings(dict(public, SUBSYNC_WYZIE_KEY="k")).conflicts())
        self.assertTrue(Settings(dict(public, SUBSYNC_TMDB_KEY="k", SUBSYNC_TMDB_PERSONAL="on")).conflicts())

    def test_public_mode_names_the_public_service(self):
        # aiosubtitles.com isn't the service; it runs at public.aiosubtitles.com
        self.assertTrue(explain.SETTINGS["SUBSYNC_PUBLIC"][1].startswith(
            "Public mode: one AIOSubtitles service for many people, like public.aiosubtitles.com. "))


# The limits' defaults and their texts as the owner approved them
APPROVED = {
    "SUBSYNC_USER_FILES_PER_DAY":
        "New files read per person per day (UTC). Files already in the shared store don't count, and neither does "
        "serving a copy. Past it, that person gets only copies the store already has until the next day; nobody else "
        "is affected. 200 is more than anyone watches in a day, with room for the files apps open without playing "
        "them; it keeps one person, or a leaked link, from using up the service's daily reads.",
    "SUBSYNC_USER_DAILY_MB":
        # with the audio route's sentence; 10,000 and 32 MB; the high-bitrate files that
        # read the whole audio limit
        "Megabytes read per person per day (UTC) through their own sync config. Reading a file's subtitle track takes "
        "about 1 MB, a few MB when the track's lines are pictures; 10,000 is 200 files at the most one file's index "
        "may take (32 MB) with room to spare, so those reads never stop someone before the file limit. A file the "
        "audio route syncs from its sound takes about 60–350 MB, so 10,000 MB covers at least 28 a day at that size, "
        "and about 10 of high-bitrate files that read the whole audio limit (1 GB by default); such a sync "
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
        "copies already in the shared store until a worker frees up. Each worker takes about 2.5 MB of memory, plus up "
        "to 32 MB for the subtitles it downloaded in the last 10 minutes, so 100 take from about 250 MB to about 3.5 "
        "GB; keep the limit within what the server's memory allows. Re-timing needs more, for a few seconds at a "
        "time: one alass run takes about 0.4 GB, up to about 1.2 GB for a 3-hour film, and at most one runs per "
        "processor, so leave about 1 GB per processor for it too.",
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
        self.assertEqual(s.user_files_per_day, 200)
        self.assertEqual(s.user_daily_bytes, 10000 * 1024 * 1024)
        self.assertEqual(s.daily_budget, int(100e9))                 # 100 GB a day
        self.assertEqual(s.max_users, 200)

    def test_the_audio_route_is_off_unless_asked_for(self):
        self.assertFalse(Settings({}).audio_route)

    def test_each_size_setting_counts_in_its_named_unit(self):
        # A *_MB setting counts MiB, a *_GB setting 10^9 bytes (settings.MB, settings.GB), each kept as it was
        from subsync.settings import GB, MB
        self.assertEqual((MB, GB), (1024 * 1024, 10 ** 9))
        s = Settings({"SUBSYNC_STORE_CAP_MB": "3", "SUBSYNC_INDEX_CAP_MB": "1.5", "SUBSYNC_SIZE_SLACK_MB": "5.5",
                      "SUBSYNC_USER_DAILY_MB": "7", "SUBSYNC_DAILY_BUDGET_GB": "2", "SUBSYNC_AUDIO_BUDGET_GB": "0.5",
                      "SUBSYNC_FILE_CAP_GB": "6"})
        self.assertEqual((s.cap_bytes, s.index_cap, s.size_slack, s.user_daily_bytes),
                         (3 * 1048576, 1572864, 5767168, 7 * 1048576))
        self.assertEqual((s.daily_budget, s.audio_budget, s.file_cap), (2 * 10 ** 9, 500_000_000, 6 * 10 ** 9))

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
        self.assertEqual(Settings({}).index_cap, 32 * 1024 * 1024)
        self.assertIn("Recommended: 32.", text)
        self.assertIn("A file whose index is larger keeps its original subtitles and isn't read again until the cap is "
                      "raised; an index over 32 MB (64 MB in an MP4 file) is never read, whatever the cap.", text)
        self.assertIn("100 is far above normal use", explain.SETTINGS["SUBSYNC_DAILY_BUDGET_GB"][1])

    def test_the_media_and_language_texts_approved_in_ad644(self):
        # The owner's words, as approved
        settings, rules = explain.SETTINGS, dict(explain.RULES)
        for key, sentence in (
                ("SUBSYNC_INDEX_CAP_MB", "A file whose index is larger keeps its original subtitles and isn't read "
                 "again until the cap is raised; an index over 32 MB (64 MB in an MP4 file) is never read, whatever "
                 "the cap. Recommended: 32. Higher, up to 64: a few more MP4 files with very many tracks get copies, "
                 "each costing that much more data."),
                ("SUBSYNC_AUDIO_BUDGET_GB", "Within it, high-bitrate files get shorter audio parts, or none when they "
                 "would be too short to judge, and then keep their originals, as do MKV files without a seek index; a "
                 "smaller file is read only as far as needed."),
                ("SUBSYNC_ORIGINAL_LANGUAGES", "Optional: titles whose original language you set yourself, which then "
                 "is never looked up, as id=language pairs separated by commas, like tt1375666=en,mal:5114=ja: an IMDb "
                 "id, or mal: or kitsu: and the number for anime known only by MyAnimeList or Kitsu (two- or "
                 "three-letter codes)."),
                ("SUBSYNC_READ_TIMEOUT_S", "How long one read of a file may wait for an answer, in seconds, before it "
                 "counts as failed; a read that keeps going counts as failed too once it has taken this long plus one "
                 "second for every 64 KB it asked for. A file's first read is then tried once more 10 seconds later, "
                 "and a failed file is tried again 10 minutes, 1, 6 and 24 hours later (a Usenet file: no second try, "
                 "then 1, 6 and 24 hours). Recommended: 60. Shorter: slow services fail more often."),
                ("SUBSYNC_TEXT_PAIRING", "A copy whose words disagree with its timing isn't offered. Chinese and "
                 "Japanese, written without spaces between words, are paired by timing alone, and nothing more is read "
                 "for them.")):
            self.assertIn(sentence, settings[key][1], key)
        self.assertIn("Other files (AVI, M2TS, fragmented MP4 and the like) keep their original subtitles, after one "
                      "small read that shows the format.", rules["Only MKV and MP4 files"])
        self.assertIn("Where the track is in the subtitle's language and timing alone leaves doubt, the lines' words "
                      "must agree too, except in Chinese and Japanese (SUBSYNC_TEXT_PAIRING);",
                      rules["A copy is offered only after it passes the check"])

    def test_public_mode_says_setup_turns_it_on(self):
        text = explain.SETTINGS["SUBSYNC_PUBLIC"][1]
        self.assertIn("Leave it off for your own install; setup turns it on when you host a public instance.", text)
        self.assertNotIn("./setup doesn't use it", text)


class Group2Test(unittest.TestCase):
    """The texts of cycle 1's vis3-explain-2, word for word."""

    def test_aiosubtitles_own_config_never_has_usenet(self):
        # it said "without Usenet unless you turn it on"; with Usenet on a second config is made;
        # the box that adds AIOSubtitles is named, not "the box"
        self.assertEqual(explain.PUBLIC_TEXTS["made_config"], "Made AIOSubtitles' own config on your AIOStreams, "
                         "without Real-Debrid, Usenet or automatic fallback to other sources.")
        self.assertIn(" to make a separate config for AIOSubtitles on your AIOStreams (without Real-Debrid, Usenet, "
                      "AIOSubtitles itself or anything that starts work in the background, so opening a file opens "
                      "only that file; with Usenet on, a second one for Usenet only) and, if you leave \"Add "
                      "AIOSubtitles to my AIOStreams config\" ticked, to add AIOSubtitles to your own config. ",
                      dict(explain.RULES)["Public mode: your AIOStreams password is used once, unless you choose to "
                                          "keep it"])
        # where the instance offers Usenet; without it, no Usenet config is spoken of
        with_usenet = explain.field_help("password", Settings({"SUBSYNC_USENET": "on"}))
        self.assertEqual(with_usenet,
                         "Your AIOStreams password, used now for two things: to add AIOSubtitles to your AIOStreams "
                         "config (if you leave \"Add AIOSubtitles to my AIOStreams config\" ticked, on the last "
                         "screen), and to make a separate AIOStreams config for AIOSubtitles without Real-Debrid, "
                         "Usenet or automatic fallback to other sources (with Usenet on, a second one for Usenet "
                         "only), so opening a file opens only that file. It is not stored unless you tick \"Keep my "
                         "password\" below. You'll be asked again only if you refresh.")
        self.assertEqual(explain.field_help("password", Settings({})), with_usenet.replace(
            " (with Usenet on, a second one for Usenet only)", ""))
        self.assertIs(explain.PUBLIC_FIELDS["password"], explain.PUBLIC_PASSWORD_TEXT)
        for text in (explain.PUBLIC_TEXTS["made_config"], explain.PUBLIC_PASSWORD_TEXT,
                     dict(explain.RULES)["Public mode: your AIOStreams password is used once, unless you choose to "
                                         "keep it"]):
            self.assertNotIn("without Usenet unless you turn it on", text)

    def test_the_settings_page_is_the_link_ending_in_configure(self):
        # "with /configure at the end" read as adding it, which gives "not found"
        self.assertEqual(explain.PUBLIC_TEXTS["notice_where"], "Open your AIOSubtitles settings page: your personal "
                         "link, ending in configure instead of manifest.json.")
        self.assertEqual(explain.PUBLIC_TEXTS["settings_link"], "It is your personal link, ending in configure "
                         "instead of manifest.json. Bookmark it to come back.")

    def test_setup_gives_its_own_reason_for_usenet_staying_off(self):
        # The public pages keep theirs
        self.assertEqual(explain.SETUP_USENET_WHY["proxy"], "Usenet stays off: subsync reads NzbDAV and AltMount only "
                         "through AIOStreams' own proxy. Turn on AIOStreams' proxy for it, or leave Usenet off.")
        self.assertEqual(explain.USENET_WHY["proxy"], "Usenet stays off: your Usenet service gives addresses on your "
                         "home network, which AIOSubtitles can't reach from the internet. Turn on AIOStreams' proxy "
                         "for it, or leave Usenet off.")
        self.assertEqual(set(explain.SETUP_USENET_WHY), set(explain.USENET_WHY))
        for why in ("nntp", "none"):
            self.assertEqual(explain.SETUP_USENET_WHY[why], explain.USENET_WHY[why])

    def test_tmdbs_answers_are_kept_up_to_six_months(self):
        # The "Risk" sentence goes; TMDB's answers expire (origlang.TMDB_KEEP_S, within its 6 months)
        from subsync import origlang
        self.assertLessEqual(origlang.TMDB_KEEP_S, 183 * 86400)
        for text in (explain.QUESTIONS["TMDB_KEY"][1], explain.SETTINGS["SUBSYNC_TMDB_KEY"][1]):
            self.assertIn(" Cost: one request per film or series every 6 months at most; it sends only the title's "
                          "IMDb id. TMDB's answers are kept up to 6 months, as its terms allow. ", text)
            self.assertNotIn("Risk:", text)
        self.assertIn(" (the answer is kept with no time limit; one from TMDB up to 6 months), ",
                      explain.SETTINGS["SUBSYNC_ORIGINAL_LANGUAGE"][1])
        tmdb = " The answers are kept with no time limit (TMDB's up to 6 months) and shared by everyone."
        for kind in ("tmdb", "personal"):
            self.assertTrue(explain.PRIVACY_LOOKUP[kind].endswith(tmdb), kind)
        self.assertTrue(explain.PRIVACY_LOOKUP["wikidata"].endswith(
            " The answers are kept with no time limit and shared by everyone."))
        self.assertNotIn("TMDB", explain.PRIVACY_LOOKUP["wikidata"])

    def test_setups_questions_state_this_installs_numbers(self):
        # The audio question's files opened a day, the audio limit's wait for a pick and Usenet's searches a
        # day; at the defaults, the words as written (README and docs/answers.example)
        for key in ("AUDIO", "AUDIO_BUDGET", "USENET", "LANGUAGES", "SUBSYNC_DAILY_BUDGET_GB"):
            with self.subTest(key=key):
                written = (explain.QUESTIONS.get(key) or explain.SETTINGS[key])[1]
                self.assertEqual(explain.setup_help(key, Settings({})), written)
                self.assertEqual(explain.setup_help(key), written)
        self.assertIn(" next are read too (at most 5 a day). ", explain.QUESTIONS["AUDIO"][1])
        self.assertIn(" take longer than the 14 seconds an app waits, ", explain.QUESTIONS["AUDIO_BUDGET"][1])
        self.assertIn(" The cost: at most 10 extra searches a day ", explain.QUESTIONS["USENET"][1])
        s = Settings({"SUBSYNC_USER_OPENED_PER_DAY": "2", "SUBSYNC_PICK_WAIT_MS": "9500",
                      "SUBSYNC_USENET_LISTINGS_PER_DAY": "7"})
        self.assertEqual(explain.setup_help("AUDIO", s), explain.QUESTIONS["AUDIO"][1].replace(
            "(at most 5 a day)", "(at most 2 a day)"))
        self.assertEqual(explain.setup_help("AUDIO_BUDGET", s), explain.QUESTIONS["AUDIO_BUDGET"][1].replace(
            "the 14 seconds", "the 9.5 seconds"))
        self.assertEqual(explain.setup_help("USENET", s), explain.QUESTIONS["USENET"][1].replace(
            "at most 10 extra searches", "at most 7 extra searches"))
        self.assertEqual(explain.setup_help("AUDIO", Settings({"SUBSYNC_USER_OPENED_PER_DAY": "0"})),
                         explain.QUESTIONS["AUDIO"][1].replace(
                             "(at most 5 a day)", "(none on this install: such files wait for a pick)"))


class Group3Test(unittest.TestCase):
    """The texts of cycle 1's vis3-explain-3, word for word."""

    @staticmethod
    def public(**env):
        return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://subs.test"}, **env))

    @staticmethod
    def privacy(s, sources=None):
        from subsync.extra import asked_sources
        return dict(explain.privacy_parts(s, asked_sources(s, "series") if sources is None else sources))

    def test_counts_of_one_read_in_the_singular(self):
        # "1 sign-up attempts", "at most 1 extra searches", "(0 of 1 searches used today)"
        self.assertEqual(explain.tries_text(1), "Each try counts: 1 sign-up attempt per address in an hour.")
        self.assertEqual(explain.tries_text(5), "Each try counts: 5 sign-up attempts per address in an hour.")
        # each look searches every indexer, so the count is each one's, not the total
        self.assertIn(" The cost: at most 1 extra search a day on each indexer ", explain.usenet_help(1))
        self.assertIn("Each look is one search per indexer in your config",
                      explain.SETTINGS["SUBSYNC_USENET_LISTINGS_PER_DAY"][1])
        self.assertNotIn("extra searches", explain.usenet_help(1))
        self.assertEqual(explain.usenet_help(10), explain.QUESTIONS["USENET"][1])
        self.assertEqual(explain.setup_help("USENET", Settings({"SUBSYNC_USENET_LISTINGS_PER_DAY": "1"})),
                         explain.usenet_help(1))
        self.assertEqual(explain.usenet_status(True, "NzbDAV", 0, 1),
                         "Usenet: on, through NzbDAV (0 of 1 search used today)")
        self.assertEqual(explain.usenet_status(True, "NzbDAV", 2, 10),
                         "Usenet: on, through NzbDAV (2 of 10 searches used today)")
        self.assertEqual(explain.usenet_status(False, "NzbDAV", 2, 10), "Usenet: off")
        self.assertEqual([explain.plural(n, "day") for n in (0, 1, 2)], ["days", "day", "days"])

    def test_the_audio_texts_at_their_zero_values(self):
        # SUBSYNC_AUDIO_BUDGET_GB 0 read "at most no set limit"; SUBSYNC_USER_AUDIO_PER_DAY 0 still asked the question
        # and said "at most 0 a day"
        self.assertIn(" 0 stops new audio syncs, takes the audio question off the pages and keeps the copies already "
                      "made. ", explain.SETTINGS["SUBSYNC_USER_AUDIO_PER_DAY"][1])
        limited, unlimited = self.public(SUBSYNC_AUDIO_ROUTE="on"), \
            self.public(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_AUDIO_BUDGET_GB="0")
        self.assertIn("about 60–350 MB a file and at most 1 GB, and works one to three minutes",
                      explain.audio_help(limited))
        self.assertIn("about 60–350 MB a file, and works one to three minutes", explain.audio_help(unlimited))
        self.assertIn("short parts of its sound (about 60–350 MB, at most 1 GB) through",
                      self.privacy(limited)["What is read"])
        self.assertIn("short parts of its sound (about 60–350 MB) through", self.privacy(unlimited)["What is read"])
        for text in (explain.audio_help(unlimited), self.privacy(unlimited)["What is read"]):
            self.assertNotIn("no set limit", text)
        self.assertIsNone(explain.audio_limit_words(unlimited))
        for gb, words in (("1", "1 GB"), ("0.5", "0.5 GB"), ("2.5", "2.5 GB"), ("1000000", "1000000 GB")):
            self.assertEqual(explain.audio_limit_words(Settings({"SUBSYNC_AUDIO_BUDGET_GB": gb})), words)
        none_a_day = self.public(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_USER_AUDIO_PER_DAY="0")
        self.assertTrue(explain.audio_offered(limited))
        self.assertFalse(explain.audio_offered(none_a_day))
        self.assertEqual(explain.intro_parts(none_a_day), explain.intro_parts(self.public()))
        self.assertEqual(explain.next_text(none_a_day), explain.PUBLIC_TEXTS["next"])
        self.assertEqual(self.privacy(none_a_day), self.privacy(self.public()))

    def test_the_kept_password_texts_state_how_often_the_check_runs(self):
        # "about once a day" whatever SUBSYNC_RECHECK_HOURS said; with 0 nothing uses a kept password; one
        # hour read "about every 1 hour"
        for hours, every in (("24", "about once a day"), ("6", "about every 6 hours"), ("48", "about every 2 days"),
                             ("36", "about every 36 hours"), ("1", "about every hour")):
            with self.subTest(hours=hours):
                s = self.public(SUBSYNC_RECHECK_HOURS=hours)
                self.assertEqual(explain.recheck_every(s), every)
                self.assertIn(f"encrypted so that it opens only with your personal link, and {every} while you use it "
                              "rebuilds its own config from yours, ", explain.field_help("keep_password", s))
                self.assertTrue(explain.PUBLIC_TEXTS["copied_auto"].format(when="today", every=every).endswith(
                    f" AIOSubtitles keeps your password and follows changes to your config by itself, {every} while "
                    "you use it."))
                self.assertTrue(s.keeps_passwords)
        self.assertFalse(self.public(SUBSYNC_RECHECK_HOURS="0").keeps_passwords)
        self.assertTrue(explain.SETTINGS["SUBSYNC_RECHECK_HOURS"][1].endswith(
            "; people who don't use the service aren't checked. With 0, the pages don't offer to keep the password, "
            "since nothing would use it."))

    def test_the_privacy_page_names_the_audio_answer_where_it_is_asked(self):
        # The answer for files with no subtitles of their own is kept in the record
        stored = "your config's id, your languages, {}and when your AIOStreams was last checked"
        answer = "your answer for files with no subtitles of their own, "
        self.assertIn(stored.format(answer), self.privacy(self.public(SUBSYNC_AUDIO_ROUTE="on"))["What is stored"])
        self.assertIn(stored.format(""), self.privacy(self.public())["What is stored"])
        self.assertIn(stored.format(answer + "your TMDB key if you added one, "),
                      self.privacy(self.public(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_TMDB_PERSONAL="on"))["What is stored"])

    def test_the_hosts_own_setup_is_the_instances_settings(self):
        # Cloudflare, 30-day backups and the project's issues were stated on every instance
        self.assertEqual(explain.SETTINGS["SUBSYNC_CONTACT"], (
            "", "Where people using this public instance can ask questions, like https://github.com/you/project/issues "
                "or mailto:you@example.com: the pages' Questions link. Empty: the pages show no Questions link."))
        self.assertEqual(explain.SETTINGS["SUBSYNC_FRONT_PROXY"], (
            "", "The name of the service in front of this instance that sees every request, like Cloudflare, for the "
                "privacy page. Empty: the privacy page says the web server in front of it is run by whoever hosts "
                "it."))
        self.assertEqual(explain.SETTINGS["SUBSYNC_BACKUP_DAYS"], (
            "", "How many days your backups keep people's encrypted records, like 30, for the privacy and settings "
                "pages. Empty: they say the host's backups may keep a deleted record longer."))
        s = Settings({"SUBSYNC_CONTACT": " mailto:a@b.test ", "SUBSYNC_FRONT_PROXY": "Cloudflare",
                      "SUBSYNC_BACKUP_DAYS": "30"})
        self.assertEqual((s.contact, s.front_proxy, s.backup_days), ("mailto:a@b.test", "Cloudflare", 30))
        self.assertEqual((Settings({}).contact, Settings({}).front_proxy, Settings({}).backup_days), ("", "", 0))
        owner, other = self.public(SUBSYNC_FRONT_PROXY="Cloudflare", SUBSYNC_BACKUP_DAYS="30"), self.public()
        self.assertEqual(self.privacy(owner)["Who else sees your requests"],
                         "AIOSubtitles runs behind Cloudflare, which passes every request on and so sees your internet "
                         "address and the links you open, your personal link included. The subtitle links your app "
                         "gets don't carry your personal link: their key opens only your subtitles, never your "
                         "settings. AIOSubtitles itself keeps no log of requests. Backups of the stored data are kept "
                         "for up to 30 days.")
        self.assertEqual(self.privacy(other)["Who else sees your requests"],
                         "The web server in front of AIOSubtitles passes every request on and so sees your internet "
                         "address and the links you open, your personal link included; whoever hosts this instance "
                         "runs it. The subtitle links your app gets don't carry your personal link: their key opens "
                         "only your subtitles, never your settings. AIOSubtitles itself keeps no log of requests. "
                         "Whoever hosts this instance decides whether backups of the stored data are made and how long "
                         "they are kept.")
        for s, field, keep, stored in (
                (owner, "(the encrypted backups keep it up to 30 days more)",
                 "; the encrypted backups keep it up to 30 days more.", "(and up to 30 days more in the encrypted backups)"),
                (other, "(the host's encrypted backups may keep it longer)",
                 "; the host's encrypted backups may keep it longer.",
                 "(and possibly longer in the host's encrypted backups)"),
                (self.public(SUBSYNC_BACKUP_DAYS="1"), "(the encrypted backups keep it up to 1 day more)",
                 "; the encrypted backups keep it up to 1 day more.", "(and up to 1 day more in the encrypted backups)")):
            with self.subTest(days=s.backup_days):
                self.assertTrue(explain.field_help("tmdb_key", s).endswith(" Remove key deletes it at once " + field
                                                                           + "."))
                self.assertTrue(explain.field_help("keep_password", s).endswith(
                    " Untick and refresh, or press \"Stop keeping my password\", to delete it at once" + keep))
                self.assertTrue(explain.field_help("forget_password", s).startswith(
                    "Deletes the AIOStreams password AIOSubtitles kept, at once " + field + "; nothing else changes."))
                self.assertIn("in the same encrypted record, until you untick it " + stored + ". ",
                              self.privacy(s)["What is stored"])
        for text in [explain.field_help(n, other) for n in ("tmdb_key", "keep_password", "forget_password")] \
                + list(self.privacy(other).values()):
            self.assertNotIn("Cloudflare", text)
            self.assertNotIn("30 days", text)

    def test_the_privacy_page_states_the_instances_record_keeping(self):
        # it said 180 days whatever SUBSYNC_USER_KEEP_DAYS was
        for days, words in (("180", "180 days"), ("30", "30 days"), ("1", "1 day")):
            with self.subTest(days=days):
                self.assertTrue(self.privacy(self.public(SUBSYNC_USER_KEEP_DAYS=days))["How long it is kept"].startswith(
                    f"until you delete your data on your settings page, or {words} after your last request; then "))

    def test_the_privacy_page_names_only_the_sites_the_instance_asks(self):
        # it named every site, and Cinemeta in each lookup sentence, even with the lookup off
        tail = (": they get the title's IMDb id, season and episode, the languages, and the file's name, size and "
                "hashes, from AIOSubtitles' server, never your internet address.")
        cinemeta = " For series, it first asks Cinemeta for TheTVDB's number by the title's id alone."
        owner = self.public(SUBSYNC_SUBDL_KEY="k", SUBSYNC_SUBSOURCE_KEY="k")
        self.assertIn(" AIOSubtitles also searches subtitle sites itself (OpenSubtitles, TheSubtitleDB, SubDL, "
                      "SubSource, Gestdown)" + tail + cinemeta + " To time subtitles to the original speech, ",
                      self.privacy(owner)["What is read"])
        self.assertIn("itself (OpenSubtitles, TheSubtitleDB, Gestdown)" + tail + cinemeta,
                      self.privacy(self.public())["What is read"])
        without_gestdown = self.privacy(self.public(SUBSYNC_EXTRA_SOURCES="osv3,tsdb"))["What is read"]
        self.assertIn("itself (OpenSubtitles, TheSubtitleDB)" + tail + " To time subtitles", without_gestdown)
        self.assertNotIn("Cinemeta", without_gestdown)
        # the Usenet words only where the instance offers Usenet
        for usenet, end in (("on", " through AIOSubtitles' own config on your AIOStreams, or from your Usenet service "
                                   "when you turn Usenet on."),
                            ("off", " through AIOSubtitles' own config on your AIOStreams.")):
            with self.subTest(usenet=usenet):
                some = self.public(SUBSYNC_EXTRA_SOURCES="osv3,tsdb", SUBSYNC_ORIGINAL_LANGUAGE="off",
                                   SUBSYNC_USENET=usenet)
                self.assertTrue(self.privacy(some)["What is read"].endswith(
                    end + " AIOSubtitles also searches subtitle sites itself (OpenSubtitles, TheSubtitleDB)" + tail))
                none = self.public(SUBSYNC_EXTRA_SOURCES="", SUBSYNC_ORIGINAL_LANGUAGE="off", SUBSYNC_USENET=usenet)
                self.assertTrue(self.privacy(none)["What is read"].endswith(end))
        self.assertEqual(explain.PRIVACY_LOOKUP["off"], "")                  # no lookup: no sentence
        for kind, where in (("tmdb", "TMDB, Wikidata, and for anime AniList and Kitsu"),
                            ("personal", "TMDB with your own key if you added one, Wikidata, and for anime AniList "
                                         "and Kitsu"),
                            ("wikidata", "Wikidata, and for anime AniList and Kitsu")):
            with self.subTest(kind=kind):
                self.assertIn(f" from its own server: at {where}. The answers are kept with no time limit",
                              explain.PRIVACY_LOOKUP[kind])
                self.assertNotIn("Cinemeta", explain.PRIVACY_LOOKUP[kind])
        self.assertIn(" Without it, your titles are looked up on Wikidata only (for anime, AniList and Kitsu). ",
                      explain.PUBLIC_FIELDS["tmdb_key"])


class Round4Test(unittest.TestCase):
    """The texts of cycle 1's round 4, word for word."""

    def test_a_real_debrid_only_install_goes_on_with_usenet(self):
        # =: the rule said "./setup stops", though with Usenet on it goes on (README's What you need)
        rule = dict(explain.RULES)["Real-Debrid is never used"]
        self.assertIn(" Real-Debrid gets a synced copy only when that service has the same file ready. With Real-Debrid "
                      "as your only debrid service, ./setup goes on only when you turn Usenet on; subsync then reads "
                      "only files you play from Usenet. subsync also checks its config ", rule)
        self.assertNotIn("./setup stops", rule)

    def test_the_check_rule_counts_the_lines_that_pair(self):
        # It said 90 % of all its lines; the check counts the lines that pair, and needs a typical error too
        from subsync import relgate
        rule = dict(explain.RULES)["A copy is offered only after it passes the check"]
        self.assertTrue(rule.startswith(
            "Each synced copy is checked against the file itself before it is offered: at least 60 % of its lines must "
            "pair with lines of the file's own subtitle track (start within a second of one), at least 90 % of those "
            "must start within 200 ms (a fifth of a second) of theirs (95 % when snapped), with a typical error of 100 "
            "ms or less, spread over the whole film, or it must be clearly better than the original (SUBSYNC_BETTER). "
            "Where the track"), rule[:200])
        self.assertEqual((relgate.MIN_PAIRED, relgate.WINDOW, relgate.WITHIN_SHARE, relgate.WITHIN_MS,
                          relgate.MEDIAN_MS, explain.SETTINGS["SUBSYNC_SNAP_WITHIN"][0]),
                         (0.6, 1.0, 0.9, 200, 100, "0.95"))                  # the numbers it gives are the check's

    def test_the_rule_on_what_is_kept_names_the_original_subtitles_kept_7_days(self):
        # The host's list of what is kept left out what the privacy page lists: each person's
        # original subtitles, and a waiting file's title, name and size, kept up to 7 days (store.KEEP_PICKABLE_S)
        from subsync.store import DAY, KEEP_PICKABLE_S
        self.assertEqual(KEEP_PICKABLE_S, 7 * DAY)
        self.assertIn(" plus how much was read for them each day. For up to 7 days after someone plays a file, the "
                      "original subtitles listed for it in their languages are kept too (and, while it waits for their "
                      "pick, its title, name and size), under a key that only their personal link leads to. Your "
                      "AIOStreams password is kept only if you tick \"Keep my password\", ",
                      dict(explain.RULES)["Public mode: what is kept, and for how long"])
        self.assertIn("For up to 7 days after you play a file, the original subtitles listed for it in your languages "
                      "are kept too (and, while it waits for your pick, its title, name and size), under a key that "
                      "only your personal link leads to.", dict(explain.PRIVACY_PARTS)["What is stored"])

    def test_a_public_record_is_deleted_as_its_setting_says_and_backups_may_keep_it(self):
        # with: it said 180 days whatever SUBSYNC_USER_KEEP_DAYS said, and left out the host's backups
        rule = dict(explain.RULES)["Public mode: what is kept, and for how long"]
        self.assertTrue(rule.endswith(
            "; logs name the titles synced, never who asked. Your record is deleted when you delete it on your settings "
            "page, or 180 days after your last request by default (SUBSYNC_USER_KEEP_DAYS); backups the host keeps may "
            "hold it longer."), rule[-200:])
        self.assertEqual(explain.SETTINGS["SUBSYNC_USER_KEEP_DAYS"][0], "180")
        self.assertEqual(Settings({}).user_keep_days, 180)

    def test_the_audio_rule_promises_no_place_of_its_own(self):
        # "so other files aren't held up" is untrue with one file at a time (SUBSYNC_AUDIO_AT_ONCE says when)
        rule = dict(explain.RULES)["Public mode: files without a subtitle track"]
        self.assertTrue(rule.endswith(
            " otherwise, or if it stops half-way, the file waits for another pick. The service runs a set number of "
            "these at once (SUBSYNC_AUDIO_AT_ONCE)."), rule[-200:])
        self.assertNotIn("held up", rule)

    def test_the_tmdb_key_text_says_it_is_the_key_that_is_never_shown(self):
        # "Its logo and notice are shown. Never shown or logged." left it unclear what is never shown
        for text in (explain.QUESTIONS["TMDB_KEY"][1], explain.SETTINGS["SUBSYNC_TMDB_KEY"][1]):
            self.assertTrue(text.endswith(
                " TMDB's answers are kept up to 6 months, as its terms allow. TMDB's logo and notice are shown; the key "
                "itself is never shown or logged."), text[-200:])
            self.assertNotIn("Never shown or logged", text)

    def test_the_daily_data_limit_counts_the_files_that_read_the_whole_audio_limit(self):
        # "at least 28 a day" holds only for files of 350 MB; at the audio limit it is about 10
        from subsync.settings import MB
        s = Settings({})
        self.assertIn(" so 10,000 MB covers at least 28 a day at that size, and about 10 of high-bitrate files that read "
                      "the whole audio limit (1 GB by default); ", explain.SETTINGS["SUBSYNC_USER_DAILY_MB"][1])
        self.assertEqual((s.user_daily_bytes // (350 * MB), round(s.user_daily_bytes / s.audio_budget),
                          explain.SETTINGS["SUBSYNC_AUDIO_BUDGET_GB"][0]), (28, 10, "1"))

    def test_the_check_and_the_kept_password_keep_usenet_to_its_own_config(self):
        # "and Usenet unless you turn it on" said Usenet goes into AIOSubtitles' config once on; the daily check
        # rebuilds the Usenet config too
        rule = dict(explain.RULES)["Public mode: your AIOStreams is checked while you use it"]
        self.assertTrue(rule.endswith(
            " press Refresh after such a change; with one, it rebuilds its configs from yours by itself, Real-Debrid "
            "always left out and Usenet kept to its Usenet config, and never changes your config."), rule[-200:])
        keep = explain.field_help("keep_password", Settings({"SUBSYNC_USENET": "on"}))
        self.assertIn(" are followed without Refresh; Real-Debrid stays out, Usenet goes only into AIOSubtitles' Usenet "
                      "config, and your own config is never changed. The cost: ", keep)
        # without Usenet on the instance, no Usenet config is spoken of
        self.assertIn(" are followed without Refresh; Real-Debrid stays out, and your own config is never changed. The "
                      "cost: ", explain.field_help("keep_password", Settings({})))
        for text in (rule, keep):
            self.assertNotIn("unless you turn it on", text)


class OffersTest(unittest.TestCase):
    """The texts of cycle 2's vis-explain-2, word for word: the public pages say only what the instance offers,
    and what a sign-up, a refresh and a delete really did."""

    @staticmethod
    def public(**env):
        return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://subs.test"}, **env))

    @staticmethod
    def privacy(s):
        from subsync.extra import asked_sources
        return dict(explain.privacy_parts(s, asked_sources(s, "series")))

    def test_with_the_check_off_no_text_asks_to_tick_keep_my_password(self):
        # With SUBSYNC_RECHECK_HOURS 0 the box isn't there, yet four texts said to tick it
        on, off = self.public(), self.public(SUBSYNC_RECHECK_HOURS="0")
        for s, password, once, refresh, stored in (
                (on, "It is not stored unless you tick \"Keep my password\" below. You'll be asked again only if you "
                     "refresh.",
                 "Your password is used for this sign-up only and is not stored, unless you tick \"Keep my password\".",
                 "It is not stored unless \"Keep my password\" is ticked.",
                 "Your AIOStreams password is stored only if you tick \"Keep my password\", in the same encrypted "
                 "record, until you untick it (and possibly longer in the host's encrypted backups). "),
                (off, "It is not stored. You'll be asked again only if you refresh.",
                 "Your password is used for this sign-up only and is not stored.", "It is not stored.",
                 "This instance doesn't keep your AIOStreams password; one kept earlier stays until you refresh or "
                 "press \"Stop keeping my password\" (and possibly longer in the host's encrypted backups). ")):
            with self.subTest(check=s.keeps_passwords):
                self.assertTrue(explain.field_help("password", s).endswith(" opens only that file. " + password))
                self.assertEqual(explain.public_text("password_once", s), once)
                self.assertTrue(explain.field_help("refresh", s).endswith(" if it is missing. " + refresh))
                self.assertIn(" come in. " + stored + "For each synced subtitle", self.privacy(s)["What is stored"])
        with_days = self.public(SUBSYNC_RECHECK_HOURS="0", SUBSYNC_BACKUP_DAYS="30")
        self.assertIn("press \"Stop keeping my password\" (and up to 30 days more in the encrypted backups). ",
                      self.privacy(with_days)["What is stored"])
        texts = [explain.field_help(n, off) for n in ("password", "refresh")]
        for text in texts + [explain.public_text("password_once", off)] + list(self.privacy(off).values()):
            self.assertNotIn("tick \"Keep my password\"", text)
            self.assertNotIn("\"Keep my password\" is ticked", text)

    def test_without_usenet_no_text_speaks_of_it_as_an_option(self):
        # With SUBSYNC_USENET off (the default) no Usenet box is shown, yet eight texts spoke of Usenet configs
        # and "when you turn Usenet on"; the stored-copies note already left its Usenet sentence out
        on, off = self.public(SUBSYNC_USENET="on", SUBSYNC_AUDIO_ROUTE="on"), self.public(SUBSYNC_AUDIO_ROUTE="on")
        self.assertEqual((on.usenet_offered, off.usenet_offered), (True, False))
        for old, new, text in (
                ("… without Real-Debrid, Usenet or automatic fallback to other sources (with Usenet on, a second one "
                 "for Usenet only), so opening a file opens only that file.",
                 "… without Real-Debrid, Usenet or automatic fallback to other sources, so opening a file opens only "
                 "that file.", lambda s: explain.field_help("password", s)),
                ("Real-Debrid stays out, Usenet goes only into AIOSubtitles' Usenet config, and your own config is "
                 "never changed.", "Real-Debrid stays out, and your own config is never changed.",
                 lambda s: explain.field_help("keep_password", s)),
                ("…, through AIOSubtitles' own config on your AIOStreams, or from your Usenet service when you turn "
                 "Usenet on.", "…, through AIOSubtitles' own config on your AIOStreams.",
                 lambda s: self.privacy(s)["What is read"]),
                ("… AIOSubtitles' own config there and its password (and its Usenet config's, with Usenet on), your "
                 "config's id, …", "… AIOSubtitles' own config there and its password, your config's id, …",
                 lambda s: self.privacy(s)["What is stored"]),
                ("AIOSubtitles' own config holds your debrid keys, as your own config does (its Usenet config your "
                 "Usenet and indexer keys), and the service can open them while your requests come in.",
                 "AIOSubtitles' own config holds your debrid keys, as your own config does, and the service can open "
                 "it while your requests come in.", lambda s: self.privacy(s)["What is stored"]),
                ("AIOSubtitles reads short parts of the file's sound through your own debrid service (or Usenet, if "
                 "you turned it on), about 60–350 MB a file…",
                 "AIOSubtitles reads short parts of the file's sound through your own debrid service, about 60–350 MB "
                 "a file…", explain.audio_help),
                ("That reads about 60–350 MB of it through your debrid service or Usenet, and the copy is there the "
                 "next time you open the file.", "That reads about 60–350 MB of it through your debrid service, and "
                 "the copy is there the next time you open the file.", lambda s: explain.intro_parts(s)[0]),
                ("… through your own debrid service or Usenet, used only to time the subtitles and not kept;",
                 "… through your own debrid service, used only to time the subtitles and not kept;",
                 lambda s: self.privacy(s)["What is read"])):
            with self.subTest(text=new[:40]):
                for s, words in ((on, old), (off, new)):
                    for part in words.strip("…").split("…"):      # the words between the proposal's "…"
                        self.assertIn(part.strip(), text(s))
                self.assertNotIn(old.strip("…").split("…")[0].strip(), text(off))
        for text in [explain.field_help(n, off) for n in ("password", "keep_password")] + [explain.audio_help(off)] \
                + list(self.privacy(off).values()) + list(explain.intro_parts(off)):
            self.assertNotIn("Usenet config", text)
            self.assertNotIn("Usenet on", text)
            self.assertNotIn("or Usenet", text)
            self.assertNotIn("Usenet service", text)

    def test_the_privacy_page_names_the_checks_read_of_the_config(self):
        # A kept password lets the check read the whole config again at every check
        for hours, every in (("24", "about once a day"), ("6", "about every 6 hours"), ("1", "about every hour")):
            with self.subTest(hours=hours):
                self.assertTrue(self.privacy(self.public(SUBSYNC_RECHECK_HOURS=hours))["What is read"].startswith(
                    "your AIOStreams config, once at sign-up and when you refresh, and, if you let AIOSubtitles keep "
                    f"your password, again {every} while you use the service; then, for each file you play, "))
        self.assertTrue(self.privacy(self.public(SUBSYNC_RECHECK_HOURS="0"))["What is read"].startswith(
            "your AIOStreams config, once at sign-up and when you refresh; then, for each file you play, "))

    def test_the_delete_texts_say_how_long_the_backups_keep_it(self):
        # Every other deletion text says how long the host's backups keep it; Delete, the biggest, didn't
        for s, backups in ((self.public(SUBSYNC_BACKUP_DAYS="30"), "the encrypted backups keep it up to 30 days more"),
                           (self.public(), "the host's encrypted backups may keep it longer")):
            with self.subTest(days=s.backup_days):
                self.assertTrue(explain.field_help("delete", s).startswith(
                    f"Deletes everything AIOSubtitles keeps about you ({backups}) and removes its own config from your "
                    "AIOStreams; your personal link stops working at once. Optional: "))
                self.assertTrue(explain.field_help("delete_stored", s).startswith(
                    f"Deletes everything AIOSubtitles keeps about you ({backups}); your personal link stops working at "
                    "once. Optional: "))
                self.assertEqual(explain.public_text("deleted", s),
                                 f"Your data is deleted ({backups}), and your personal link no longer works.")

    def test_a_change_sent_but_not_checked_and_the_configs_a_delete_left(self):
        # "nothing there was changed" also when the save went through;: the note named one config
        # and debrid keys, whichever were left
        self.assertEqual(explain.PUBLIC_TEXTS["not_added"],
                         "AIOSubtitles couldn't add itself to your AIOStreams config, so nothing there was changed.")
        self.assertEqual(explain.PUBLIC_UNCHECKED_LEAD,
                         "AIOSubtitles sent its change to your AIOStreams config but couldn't check that it went "
                         "through. If AIOSubtitles isn't among the addons there, add it yourself:")
        self.assertEqual(explain.PUBLIC_REFRESH_UNCHECKED,
                         "AIOSubtitles' config on your AIOStreams now matches yours, but AIOSubtitles couldn't check "
                         "that it is back in your own config. If it isn't among the addons there, add it yourself:")
        self.assertEqual(explain.PUBLIC_TEXTS["sync_left"],
                         "AIOSubtitles couldn't remove its own config from your AIOStreams. It stays there unused and "
                         "holds the same debrid keys as your config; whoever runs your AIOStreams can delete it.")
        self.assertEqual(explain.PUBLIC_TEXTS["usenet_left"],
                         "AIOSubtitles couldn't remove its Usenet config from your AIOStreams. It stays there unused "
                         "and holds the same Usenet and indexer keys as your config; whoever runs your AIOStreams can "
                         "delete it.")
        self.assertEqual(explain.PUBLIC_TEXTS["configs_left"],
                         "AIOSubtitles couldn't remove its two configs from your AIOStreams. They stay there unused "
                         "and hold the same debrid, Usenet and indexer keys as your config; whoever runs your "
                         "AIOStreams can delete them.")


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


class NumbersTest(unittest.TestCase):
    """Each number a text states that a code constant holds is that constant's (the texts keep their approved
    words; a changed constant fails here until its text says the same)."""

    def test_a_file_that_cant_be_reached(self):
        from subsync.store import READ_FAILED_BACKOFF_S
        from subsync.worker import READ_RETRY_S
        first, *hours = READ_FAILED_BACKOFF_S
        later = ", ".join(str(h // 3600) for h in hours[:-1]) + f" and {hours[-1] // 3600} hours"
        self.assertIn(f"tried once more {READ_RETRY_S} seconds later; if it still fails, it waits {first // 60} "
                      f"minutes, then {later}, and then isn't read again.",
                      dict(explain.RULES)["A file that can't be reached is tried again later"])
        self.assertIn(f"tried once more {READ_RETRY_S} seconds later, and a failed file is tried again {first // 60} "
                      f"minutes, {later} later", explain.SETTINGS["SUBSYNC_READ_TIMEOUT_S"][1])

    def test_a_usenet_file_that_cant_be_reached(self):
        # Both texts gave debrid's waits only; a Usenet file gets no second try (worker: `not job.usenet`),
        # Usenet stops until 00:00 UTC, and the file waits REFUSED_BACKOFF_S
        from subsync.store import REFUSED_BACKOFF_S
        *firsts, last = (h // 3600 for h in REFUSED_BACKOFF_S)
        waits = ", ".join(str(h) for h in firsts) + f" and {last} hours"
        self.assertEqual(waits, "1, 6 and 24 hours")
        self.assertTrue(dict(explain.RULES)["A file that can't be reached is tried again later"].endswith(
            "and then isn't read again. A Usenet file gets no second try: nothing more is looked for on Usenet until "
            f"00:00 UTC, and the file waits {waits}."))
        self.assertIn("A file's first read is then tried once more 10 seconds later, and a failed file is tried again "
                      f"10 minutes, 1, 6 and 24 hours later (a Usenet file: no second try, then {waits}). Recommended: "
                      "60.", explain.SETTINGS["SUBSYNC_READ_TIMEOUT_S"][1])

    def test_a_refused_file(self):
        from subsync.store import REFUSED_BACKOFF_S
        hours = [h // 3600 for h in REFUSED_BACKOFF_S]
        waits = ", ".join(str(h) for h in hours[:-1]) + f", then {hours[-1]} hours"
        self.assertIn(f"the refused file itself waits {waits}.", explain.SETTINGS["SUBSYNC_403_BACKOFF_S"][1])

    def test_a_failing_site_is_paused(self):
        from subsync import srclimits as sl
        longest = {3600: "an hour"}[sl.RETRY_AFTER_MAX_S]
        self.assertIn(f"fails {WORDS[sl.PAUSE_FAILURES]} times within {WORDS[sl.PAUSE_WINDOW_S // 60]} minutes (no "
                      f"answer, or an error) is skipped for {WORDS[sl.PAUSE_S // 60]} minutes, then tried with one "
                      f"request: if it answers, it is used again; if not, it waits another {WORDS[sl.PAUSE_S // 60]} "
                      f"minutes. A site that asks AIOSubtitles to slow down waits as long as it asks, at most "
                      f"{longest}.",
                      dict(explain.RULES)["A failing subtitle site is paused"])
        # An answer that takes longer than srclimits.ANSWER_WAIT_MAX_S counts as failing
        minute = {60: "a minute"}[sl.ANSWER_WAIT_MAX_S]
        self.assertIn(f"A site that answers later, within {minute}, is left out for this play",
                      explain.SETTINGS["SUBSYNC_SOURCE_TIMEOUT_S"][1])
        self.assertIn(f"fails {WORDS[sl.PAUSE_FAILURES]} times in {WORDS[sl.PAUSE_WINDOW_S // 60]} minutes (an answer "
                      f"that takes more than {minute} counts as failing) is paused for {WORDS[sl.PAUSE_S // 60]} "
                      "minutes", explain.SETTINGS["SUBSYNC_SOURCE_TIMEOUT_S"][1])

    def test_the_sites_limits_and_the_subtitles_a_file_takes(self):
        from subsync import srclimits as sl
        per_file, limits = sl.PER_FILE_DOWNLOADS, sl.LIMITS
        day = 86400

        def per(key, window):
            return f"{dict(limits[key])[window]:,}"
        self.assertIn(f"SubDL {per(('subdl', 'download', False), day)} downloads a day for each internet address and "
                      f"{per(('subdl', 'request', True), day)} searches a day for each key, SubSource "
                      f"{per(('subsource', 'request', True), day)} requests a day, Wyzie "
                      f"{per(('wyzie', 'request', True), day)}. AIOSubtitles counts them and takes at most "
                      f"{WORDS[per_file['subdl']]} subtitles per file and language from SubDL, "
                      f"{WORDS[per_file['subsource']]} from SubSource and {WORDS[per_file['wyzie']]} from Wyzie",
                      dict(explain.RULES)["Sites with limits give each file only a few subtitles"])
        self.assertIn(f"(at most {WORDS[per_file['subdl']]} per file and language)", explain.QUESTIONS["SUBDL_KEY"][1])
        self.assertIn(f"(at most {WORDS[per_file['wyzie']]} per file and language)", explain.QUESTIONS["WYZIE_KEY"][1])
        subsource = explain.SETTINGS["SUBSYNC_SUBSOURCE_KEY"][1]
        subsource_key = ("subsource", "request", True)
        self.assertIn(f"({per(subsource_key, 60)} a minute, {per(subsource_key, day)} a day for a free key); at most "
                      f"{WORDS[per_file['subsource']]} subtitles per file and language", subsource)
        self.assertIn(f"from {per(('tsdb', 'download', False), 3600)} an hour to "
                      f"{per(('tsdb', 'download', True), day)} a day", explain.SETTINGS["SUBSYNC_TSDB_KEY"][1])

    def test_the_size_slacks_rounding_is_in_its_own_unit(self):
        # Half of 0.01 GiB was given in decimal MB (5.4) beside a setting counted in MiB, where it is 5.1
        from subsync.settings import MB
        half = 0.01 * 1024 * MB / 2 / MB
        self.assertIn(f"listings round sizes to 0.01 GiB, up to {half:.1f} MB off. Recommended: 5.5.",
                      explain.SETTINGS["SUBSYNC_SIZE_SLACK_MB"][1])
        self.assertEqual(f"{half:.1f}", "5.1")
        self.assertEqual(Settings({}).size_slack, int(float(explain.SETTINGS["SUBSYNC_SIZE_SLACK_MB"][0]) * MB))

    def test_a_workers_memory(self):
        # The text said about 2.5 MB a worker, leaving out the downloads each keeps for the audio job after a
        # pick; its numbers are worker.FETCHED_CAP and worker.LIST_TTL_S
        from subsync.settings import MB
        from subsync.worker import FETCHED_CAP, LIST_TTL_S
        self.assertIn(f"plus up to {FETCHED_CAP // MB} MB for the subtitles it downloaded in the last "
                      f"{LIST_TTL_S // 60} minutes, so 100 take", explain.SETTINGS["SUBSYNC_ACTIVE_WORKERS"][1])
        # Re-timing's memory is said too; "at most one runs per processor" is alass's own bound
        from subsync import alass
        from subsync.settings import available_cpus
        self.assertIn("and at most one runs per processor, so leave about 1 GB per processor for it too.",
                      explain.SETTINGS["SUBSYNC_ACTIVE_WORKERS"][1])
        self.assertEqual(alass.RUNS_AT_ONCE, available_cpus())

    def test_the_language_check_and_the_shared_timing(self):
        from subsync.langcheck import MIN_WORDS
        from subsync.store import KEEP_SERVED_S
        self.assertIn(f"once it has about {MIN_WORDS} words)", dict(explain.RULES)["Which subtitles are tried first"])
        days = f"kept {KEEP_SERVED_S // 86400} days after last use"
        self.assertIn(days, dict(explain.RULES)["Public mode: what is kept, and for how long"])
        self.assertIn(days, dict(explain.PRIVACY_PARTS)["What is stored"])

    def test_what_is_read_of_a_file(self):
        # The index cap's, read timeout's and text pairing's numbers were typed in words only
        from subsync import mkv, mp4, reader, textpair
        from subsync.settings import MB
        index = explain.SETTINGS["SUBSYNC_INDEX_CAP_MB"][1]
        self.assertIn(f"(the start, the index, and {mkv.HASH_CHUNK // 1024} KB at each end)", index)
        self.assertIn(f"an index over {mkv.MAX_CUES // MB} MB ({mp4.MAX_MOOV // MB} MB in an MP4 file) is never read, "
                      f"whatever the cap. Recommended: 32. Higher, up to {mp4.MAX_MOOV // MB}:", index)
        self.assertIn(f"plus one second for every {reader.READ_BYTES_PER_S // 1024} KB it asked for.",
                      explain.SETTINGS["SUBSYNC_READ_TIMEOUT_S"][1])
        self.assertIn(f"reads the words of up to {textpair.MAX_LINES} of the track's lines, once per file and language "
                      f"(up to {textpair.MAX_LINES * mkv.WORD_READ // 1024} KB of text; on an image track, with a reader "
                      f"installed for the language, each line's picture, up to {mkv.IMAGE_READ // 1024} KB)",
                      explain.SETTINGS["SUBSYNC_TEXT_PAIRING"][1])

    def test_what_is_kept_and_what_waits(self):
        # The store's 180 days beside its cap, and a file Usenet doesn't have, waiting a day
        from subsync.store import KEEP_SERVED_S
        from subsync.worker import USENET_UNLISTED_S
        self.assertIn(f"what nobody used for {KEEP_SERVED_S // 86400} days is deleted",
                      explain.SETTINGS["SUBSYNC_STORE_CAP_MB"][1])
        day = {86400: "a day"}[USENET_UNLISTED_S]
        self.assertIn(f"A file it doesn't find there isn't looked for again for {day},",
                      dict(explain.RULES)["Usenet: only parts of a file, only when you turn it on"])

    def test_the_privacy_page_names_the_rows_kept_for_a_person_alone(self):
        # The files a person waits to pick and the original subtitles listed for what they play were kept
        # (for up to 7 days, Delete or not) and not named among what is stored
        from subsync.store import KEEP_PICKABLE_S
        for s in (Group3Test.public(), Group3Test.public(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_TMDB_PERSONAL="on")):
            self.assertIn(
                "and how much was read for you each day. For up to "
                f"{KEEP_PICKABLE_S // 86400} days after you play a file, the original subtitles listed for it in your "
                "languages are kept too (and, while it waits for your pick, its title, name and size), under a key "
                "that only your personal link leads to. AIOSubtitles' own config holds your debrid keys, as your own "
                "config does", Group3Test.privacy(s)["What is stored"])

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_unreachable_state(self):
        from subsync.recheck import UNREACHABLE_CHECKS
        self.assertTrue(explain.PUBLIC_STATES["unreachable"].text.startswith(
            f"The last {WORDS[UNREACHABLE_CHECKS]} checks couldn't reach your AIOStreams"))


if __name__ == "__main__":
    unittest.main()
