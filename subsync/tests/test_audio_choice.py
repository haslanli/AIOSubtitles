"""Each person's audio choice: off, on pick (the default) or as soon as a file is opened, behind the
instance's audio route; the audio syncs a person may start a day and the audio places as settings; Usenet files in the
audio route. Plain Python except where marked; no network beyond 127.0.0.1; every run bounded (ulimit -t)."""
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import (closed_after, Counts, FakeDebrid, FakeUsenet, FIXTURES, HAVE_CRYPTOGRAPHY, KEY, Limits, Listing,
                     NEEDS, RangeServer, row, start_server, stop_server, TempDir, wait_until)
from subsync import sources
from subsync import worker as worker_mod
from subsync.reader import ReadFailed, Refused
from subsync.settings import Settings
from subsync.store import Store
from subsync.worker import Job, Safety, StoreAudioCounts, Worker, audio_places



# ---------------------------------------------------------------------------------------------------- the settings

AUDIO_WHEN_TEXT = (
    "Pick or open: when a file with no subtitle track of its own starts its audio sync, with SUBSYNC_AUDIO_ROUTE on. "
    "pick (recommended): only after its \"(Subsync)\" entry is picked. open: as soon as an app asks for the file's "
    "subtitles, if no synced copy exists, a subtitle exists to fit and the day's data has room, at most "
    "SUBSYNC_USER_OPENED_PER_DAY a day; it reads 60–350 MB of files nobody may watch. A public instance ignores it: "
    "each person chooses on their settings page.")
AUDIO_PER_DAY_TEXT = (
    "Audio syncs one person may start a day (UTC), picked or opened. Each reads about 60–350 MB of a file's sound and "
    "keeps a processor busy for one to three minutes, and its copy then serves everyone who plays that file. 20 by "
    "default; recommended: keep it, so the processors are shared fairly while the daily data limit bounds the "
    "reads. 0 stops new audio syncs, takes the audio question off the pages and keeps the copies already made. Past "
    "it, that person's files wait for a pick the next day.")     # the pages treat 0 as the route off
OPENED_PER_DAY_TEXT = (
    "Of those, how many may start as soon as a file is opened, for people who chose that (UTC day). 5 by default; "
    "recommended: keep it low, since apps also ask for files people only browse or that they prepare next. 0 "
    "removes that answer from the pages; people who had chosen it then sync when they pick.")
# with one place, an audio sync holds it and other files wait (audio_places(1) is 1)
AUDIO_AT_ONCE_TEXT = (
    "Audio syncs running at the same time, for everyone together. auto (the default, recommended) is half of "
    "SUBSYNC_SYNCS_AT_ONCE, at least one, so ordinary files never wait behind them; with one file at a time "
    "(SUBSYNC_SYNCS_AT_ONCE 1, or auto on one processor), they wait while an audio sync runs. A higher number finishes "
    "picked files sooner when many come together but makes every other file wait longer; it can't be more than "
    "SUBSYNC_SYNCS_AT_ONCE. A self-hosted install syncs one at a time whatever it says.")


class SettingsTest(unittest.TestCase):
    def test_the_defaults(self):
        s = Settings({})
        self.assertEqual((s.audio_when, s.user_audio_per_day, s.user_opened_per_day, s.audio_at_once),
                         ("pick", 20, 5, "auto"))
        self.assertEqual(s.audio_choice, "off")                        # the audio route itself stays off by default
        self.assertEqual(Settings({"SUBSYNC_AUDIO_ROUTE": "on"}).audio_choice, "pick")

    def test_when_and_the_old_on_pick_setting(self):
        when = lambda **env: Settings(dict({"SUBSYNC_AUDIO_ROUTE": "on"}, **env)).audio_choice  # noqa: E731
        self.assertEqual(when(SUBSYNC_AUDIO_WHEN="open"), "open")
        self.assertEqual(when(SUBSYNC_AUDIO_WHEN=" Pick "), "pick")
        self.assertEqual(when(SUBSYNC_AUDIO_ON_PICK="off"), "open")     # still read: off means open
        self.assertEqual(when(SUBSYNC_AUDIO_ON_PICK="on"), "pick")
        self.assertEqual(when(SUBSYNC_AUDIO_ON_PICK="off", SUBSYNC_AUDIO_WHEN="pick"), "pick")   # the new one decides
        self.assertEqual(Settings({"SUBSYNC_AUDIO_WHEN": "open"}).audio_choice, "off")       # the route decides first

    def test_the_counts_and_places(self):
        s = Settings({"SUBSYNC_USER_AUDIO_PER_DAY": "0", "SUBSYNC_USER_OPENED_PER_DAY": "0", "SUBSYNC_AUDIO_AT_ONCE": "3"})
        self.assertEqual((s.user_audio_per_day, s.user_opened_per_day, s.audio_at_once), (0, 0, 3))
        self.assertEqual(Settings({"SUBSYNC_AUDIO_AT_ONCE": " AUTO "}).audio_at_once, "auto")
        self.assertIsNone(Settings({"SUBSYNC_AUDIO_AT_ONCE": "0"}).audio_at_once)
        self.assertEqual([audio_places(n) for n in (1, 2, 3, 4, 8)], [1, 1, 1, 2, 4])          # auto: as before
        self.assertEqual([audio_places(4, 3), audio_places(4, 1), audio_places(None, "auto")], [3, 1, 1])

    def test_the_conflicts(self):
        public = {"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_SYNCS_AT_ONCE": "2"}
        # the person's choice replaces "on pick must stay on": the old setting no longer stops a public instance
        self.assertEqual(Settings(dict(public, SUBSYNC_AUDIO_ON_PICK="off")).conflicts(), [])
        self.assertEqual(Settings(dict(public, SUBSYNC_AUDIO_AT_ONCE="2")).conflicts(), [])
        over = Settings(dict(public, SUBSYNC_AUDIO_AT_ONCE="3")).conflicts()
        self.assertEqual(len(over), 1)
        self.assertIn("SUBSYNC_AUDIO_AT_ONCE", over[0])
        self.assertIn("SUBSYNC_SYNCS_AT_ONCE (2)", over[0])
        # self-hosted: one audio lane whatever it says, so a larger number is not refused
        self.assertEqual(Settings({"SUBSYNC_SYNCS_AT_ONCE": "2", "SUBSYNC_AUDIO_AT_ONCE": "3"}).conflicts(), [])
        for env in ({"SUBSYNC_AUDIO_AT_ONCE": "half"}, {"SUBSYNC_AUDIO_WHEN": "always"}):
            with self.subTest(env=env):
                out = Settings(env).conflicts()
                self.assertEqual(len(out), 1)
                self.assertIn(next(iter(env)), out[0])

    def test_the_texts(self):
        from subsync import explain
        for name, default, text in (("SUBSYNC_AUDIO_WHEN", "pick", AUDIO_WHEN_TEXT),
                                    ("SUBSYNC_USER_AUDIO_PER_DAY", "20", AUDIO_PER_DAY_TEXT),
                                    ("SUBSYNC_USER_OPENED_PER_DAY", "5", OPENED_PER_DAY_TEXT),
                                    ("SUBSYNC_AUDIO_AT_ONCE", "auto", AUDIO_AT_ONCE_TEXT)):
            with self.subTest(name=name):
                self.assertEqual(explain.SETTINGS[name], (default, text))
        for name in ("SUBSYNC_AUDIO_ROUTE", "SUBSYNC_AUDIO_ON_PICK", "SUBSYNC_PUBLIC"):
            with self.subTest(name=name):
                self.assertNotIn("only after a pick", explain.SETTINGS[name][1])
                self.assertNotIn("must stay on", explain.SETTINGS[name][1])
        self.assertIn("their own debrid service or Usenet", explain.SETTINGS["SUBSYNC_AUDIO_ROUTE"][1])


# --------------------------------------------------------------------------------------- the self-hosted day counts

class StoreCountsTest(unittest.TestCase):
    def test_counted_in_the_stores_day_counters(self):
        with TempDir() as d:
            store = closed_after(self, Store(d))
            s = Settings({"SUBSYNC_USER_AUDIO_PER_DAY": "3", "SUBSYNC_USER_OPENED_PER_DAY": "1"})
            counts = StoreAudioCounts(store, s)
            self.assertTrue(counts.may_start_audio(opened=True))
            counts.audio_started(opened=True)                      # an opened sync counts one of each
            self.assertEqual((store.day_count("audio_syncs"), store.day_count("audio_opened")), (1, 1))
            self.assertFalse(counts.may_start_audio(opened=True))   # the opened ones are used up
            self.assertTrue(counts.may_start_audio(opened=False))
            counts.audio_started()                                 # a picked one counts one
            counts.audio_started()
            self.assertEqual((store.day_count("audio_syncs"), store.day_count("audio_opened")), (3, 1))
            self.assertFalse(counts.may_start_audio(opened=False))
            s.user_audio_per_day = 0                                # read when asked: 0 stops new audio syncs
            self.assertFalse(StoreAudioCounts(closed_after(self, Store(d)), s).may_start_audio())


# --------------------------------------------------------------------------------------------- each person's record

def record(audio=None):
    out = {"instance": "https://aios.test", "sync_uuid": "u1", "sync_link": "https://aios.test/stremio/u1/E1/manifest.json",
           "sync_password": "p1", "languages": ["eng"], "version": 1}
    if audio is not None:
        out["audio"] = audio
    return out


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class RecordTest(unittest.TestCase):
    def setUp(self):
        from subsync.users import Users, new_token
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.t = [1_800_000_000.0]
        self.env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                    "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_ORIGINAL_LANGUAGE": "off", "SUBSYNC_AUDIO_ROUTE": "on",
                    "SUBSYNC_SYNCS_AT_ONCE": "4"}
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), Settings(self.env).secret_bytes(),
                           clock=lambda: self.t[0])
        self.addCleanup(self.users.close)
        self.token = new_token()
        self.users.create(self.token, record())

    def test_a_missing_key_reads_pick_and_the_choice_reaches_the_worker(self):
        from subsync.public import person_audio, user_settings
        s = Settings(self.env)
        self.assertEqual(person_audio(record(), s), "pick")             # everyone signed up before, the owner included
        self.assertEqual([person_audio(record(a), s) for a in ("off", "pick", "open", "sometimes")],
                         ["off", "pick", "open", "pick"])
        none_opened = Settings(dict(self.env, SUBSYNC_USER_OPENED_PER_DAY="0"))
        self.assertEqual(person_audio(record("open"), none_opened), "pick")   # the answer is gone: they sync on a pick
        for audio, choice in ((None, "pick"), ("off", "off"), ("open", "open")):
            with self.subTest(audio=audio):
                # the instance's own SUBSYNC_AUDIO_ON_PICK or SUBSYNC_AUDIO_WHEN changes nothing for a person
                env = dict(self.env, SUBSYNC_AUDIO_ON_PICK="off", SUBSYNC_AUDIO_WHEN="pick")
                self.assertEqual(user_settings(env, record(audio), self.token).audio_choice, choice)
        self.assertEqual(user_settings(dict(self.env, SUBSYNC_AUDIO_ROUTE="off"), record("open"),
                                       self.token).audio_choice, "off")

    def test_the_day_table_counts_per_utc_day_and_goes_with_the_person(self):
        import sqlite3
        self.assertEqual(self.users.audio_usage(self.token), {"syncs": 0, "opened": 0})
        self.users.add_audio(self.token)
        self.users.add_audio(self.token, opened=True)
        self.assertEqual(self.users.audio_usage(self.token), {"syncs": 2, "opened": 1})
        self.t[0] += 86400                                              # the next UTC day starts at zero
        self.assertEqual(self.users.audio_usage(self.token), {"syncs": 0, "opened": 0})
        self.users.add_audio(self.token)
        db = sqlite3.connect(os.path.join(self.dir, "users.sqlite3"))
        self.addCleanup(db.close)
        # a table of its own: the usage table keeps its four columns, so an older image still opens the database
        self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(usage)")], ["id", "day", "files", "bytes"])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audio_usage").fetchone()[0], 2)
        self.assertTrue(self.users.delete(self.token))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audio_usage").fetchone()[0], 0)
        from subsync.users import new_token
        other = new_token()
        self.users.create(other, record())
        self.users.add_audio(other, opened=True)
        self.t[0] += 200 * 86400                                        # unused for longer than the keep days
        self.assertEqual(self.users.expire(180), 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audio_usage").fetchone()[0], 0)

    def test_the_persons_limits(self):
        from subsync.public import StoreOnly, UserLimits
        limits = UserLimits(Settings(dict(self.env, SUBSYNC_USER_AUDIO_PER_DAY="3", SUBSYNC_USER_OPENED_PER_DAY="1")),
                            self.users, self.token)
        self.assertTrue(limits.may_start_audio(opened=True))
        limits.audio_started(opened=True)                               # counts one of each
        self.assertEqual(self.users.audio_usage(self.token), {"syncs": 1, "opened": 1})
        self.assertFalse(limits.may_start_audio(opened=True))
        limits.audio_started()                                          # a picked one counts one
        limits.audio_started()
        self.assertEqual(self.users.audio_usage(self.token), {"syncs": 3, "opened": 1})
        self.assertFalse(limits.may_start_audio())
        self.assertFalse(StoreOnly().may_start_audio())

    def test_the_audio_places_come_from_the_setting(self):
        from subsync.public import Pool
        for at_once, places in (("auto", 2), ("3", 3), ("1", 1)):
            with self.subTest(at_once=at_once):
                env = dict(self.env, SUBSYNC_AUDIO_AT_ONCE=at_once)
                pool = Pool(Settings(env), env, closed_after(self, Store(self.dir)), self.users, start_workers=False)
                self.addCleanup(pool.close)
                self.assertEqual((pool.turn.places, pool.audio_turn.places), (4, places))
                self.assertIs(pool.app(self.token).worker.audio_turn, pool.audio_turn)

    def test_the_settings_action_saves_the_choice(self):
        from subsync.signup import SignupError, set_audio
        s = Settings(self.env)
        self.assertEqual(set_audio(self.users, self.token, "open", s), "open")
        self.assertEqual(self.users.open(self.token)["audio"], "open")
        self.assertEqual(set_audio(self.users, self.token, "off", s), "off")
        for value, settings in (("sometimes", s), ("", s), (None, s),
                                ("open", Settings(dict(self.env, SUBSYNC_USER_OPENED_PER_DAY="0")))):
            with self.subTest(value=value):
                with self.assertRaises(SignupError):
                    set_audio(self.users, self.token, value, settings)
                self.assertEqual(self.users.open(self.token)["audio"], "off")
        with self.assertRaises(SignupError):                           # an instance without the audio route
            set_audio(self.users, self.token, "pick", Settings(dict(self.env, SUBSYNC_AUDIO_ROUTE="off")))


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class ConfigureTest(unittest.TestCase):
    """The settings page's "audio" action: saved like the languages, and the person's worker starts again with it."""

    def setUp(self):
        import test_public_http as harness
        self.harness = harness
        self.case = harness.Base()
        self.case.start(SUBSYNC_AUDIO_ROUTE="on")
        self.addCleanup(self.case.tearDown)

    def post(self, token, **fields):
        h = self.harness
        return h.http(f"{self.case.base}/u/{token}/configure", h.form(**fields))

    def test_saved_and_applied(self):
        c = self.case
        _, _, _, _, token = c.sign_up()
        self.assertEqual(c.pool.app(token).s.audio_choice, "pick")      # nothing chosen yet: on pick
        code, _, headers = self.post(token, action="audio", audio="open")
        self.assertEqual((code, headers["Location"]), (303, "configure?done=audio"))
        self.assertEqual(c.users.open(token)["audio"], "open")
        self.assertEqual(c.pool.app(token).s.audio_choice, "open")      # the worker uses it from now on
        code, _, _ = self.post(token, action="audio", audio="loud")
        self.assertEqual(code, 400)
        self.assertEqual(c.users.open(token)["audio"], "open")


# ------------------------------------------------------------------------------------------------- the worker's side

SUB = {"id": "os-1", "lang": "eng", "release": "", "hash": "", "hash_match": False, "ai": False}


class Base(unittest.TestCase):
    choice = "open"

    def setUp(self):
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.store = closed_after(self, Store(self.dir))
        self.workers = []
        self.addCleanup(lambda: [w.stop() for w in self.workers])
        self.srv = RangeServer(FIXTURES)
        self.addCleanup(self.srv.close)
        self.size = os.path.getsize(os.path.join(FIXTURES, "tiny-nosub.mp4"))
        self.hints = {"filename": "tiny-nosub.mp4", "videoSize": self.size}
        self.alias = Job("movie", "tt1", self.hints).alias

    def settings(self, choice=None, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://x.test", "SUBSYNC_PUBLIC": "on",
                           "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_EXTRA_SOURCES": ""}, **env))
        s.audio_when = choice or self.choice          # the person's own choice (public.user_settings sets it)
        return s

    def subs(self, *langs, **extra):
        return [dict(SUB, id=f"os-{lang}", lang=lang, url=self.srv.url("tiny.srt"), **extra) for lang in langs or ("eng",)]

    def worker(self, limits=None, settings=None, subs=None, row=None, **kw):
        listing = Listing(row or {"url": self.srv.url("tiny-nosub.mp4")}, self.subs() if subs is None else subs)
        w = Worker(settings or self.settings(), self.store, listing, Safety(), limits=limits, **kw)
        self.workers.append(w)
        return w

    def first_pass(self, w):
        w.process(Job("movie", "tt1", self.hints))
        return w

    def queued_audio(self, w):
        return [(j.alias, j.audio, j.opened) for j in w.jobs.values()]

    def file_key(self):
        return self.store.resolve(self.alias)


class OpenedTest(Base):
    """"as soon as a file is opened" starts where a file with no usable track would wait for a pick, when every
    point holds; when one fails the file waits for a pick, as "when I pick it" does."""

    def test_every_point_holds_the_audio_sync_starts_at_once(self):
        limits = Limits()
        w = self.first_pass(self.worker(limits))
        self.assertEqual(self.queued_audio(w), [(self.alias, True, True)])   # an audio job, on the audio lane
        self.assertFalse(w.waits_for_pick(self.alias))
        self.assertEqual((limits.syncs, limits.opened), (0, 0))               # counted when it starts, not before
        self.assertEqual(w._next("audio").alias, self.alias)

    def test_point_1_a_file_with_its_own_track_is_synced_from_it(self):
        srv_file = "tiny.mkv"
        w = self.worker(Limits(), row={"url": self.srv.url(srv_file)})
        w._sync_all = lambda *a: None
        w.process(Job("movie", "tt1", {"filename": srv_file, "videoSize": os.path.getsize(os.path.join(FIXTURES, srv_file))}))
        self.assertEqual(len(w.jobs), 0)
        self.assertFalse(w.waits_for_pick(f"name:{srv_file}|{os.path.getsize(os.path.join(FIXTURES, srv_file))}"))

    def test_point_2_a_copy_or_an_audio_verdict_in_every_language_waits_for_a_pick(self):
        for outcome in ("synced", "keep_original"):
            with self.subTest(outcome=outcome):
                self.store.db.execute("DELETE FROM pickable")
                self.store.db.execute("DELETE FROM results")
                self.store.db.execute("DELETE FROM offers")
                w = self.first_pass(self.worker(Limits()))          # the file is known now; its job is dropped below
                w.jobs.clear()
                w.queue.clear()
                key = self.store.put(self.file_key(), "a" * 64, "eng", 0, outcome, "x", route="audio", body="1\n",
                                     fmt="srt", candidate_id="os-eng", original="1\n")
                if outcome == "synced":
                    self.store.set_offers(self.file_key(), "eng", [key])
                w2 = self.first_pass(self.worker(Limits()))
                self.assertEqual(len(w2.jobs), 0)
                self.assertTrue(w2.waits_for_pick(self.alias))

    def test_point_2_one_language_without_a_copy_is_enough(self):
        s = self.settings(SUBSYNC_LANGUAGES="eng,spa")
        w = self.first_pass(self.worker(Limits(), settings=s, subs=self.subs("eng", "spa")))
        w.jobs.clear()
        w.queue.clear()
        key = self.store.put(self.file_key(), "a" * 64, "eng", 0, "synced", "x", route="audio", body="1\n", fmt="srt",
                             candidate_id="os-eng", original="1\n")
        self.store.set_offers(self.file_key(), "eng", [key])
        w2 = self.first_pass(self.worker(Limits(), settings=s, subs=self.subs("eng", "spa")))
        self.assertEqual(self.queued_audio(w2), [(self.alias, True, True)])

    def test_point_3_no_subtitle_to_fit_in_the_languages_without_a_copy_waits_for_a_pick(self):
        s = self.settings(SUBSYNC_LANGUAGES="eng,spa")
        subs = self.subs("eng") + [dict(SUB, id="os-spa", lang="spa", url=self.srv.url("tiny.srt"), ai=True)]
        w = self.first_pass(self.worker(Limits(), settings=s, subs=self.subs("eng")))
        w.jobs.clear()
        w.queue.clear()
        key = self.store.put(self.file_key(), "a" * 64, "eng", 0, "synced", "x", route="audio", body="1\n", fmt="srt",
                             candidate_id="os-eng", original="1\n")
        self.store.set_offers(self.file_key(), "eng", [key])
        w2 = self.first_pass(self.worker(Limits(), settings=s, subs=subs))   # Spanish: only an AI-made one
        self.assertEqual(len(w2.jobs), 0)
        self.assertTrue(w2.waits_for_pick(self.alias))

    def test_point_4_no_room_in_the_day_or_the_counts_used_up_waits_for_a_pick(self):
        for limits in (Limits(room=False), Limits(per_day=0), Limits(opened_per_day=0)):
            with self.subTest(room=limits.room, per_day=limits.per_day, opened=limits.opened_per_day):
                self.store.db.execute("DELETE FROM pickable")
                w = self.first_pass(self.worker(limits))
                self.assertEqual(len(w.jobs), 0)
                self.assertTrue(w.waits_for_pick(self.alias))
        w = self.worker(Limits())
        w._audio_need = lambda alias: w.s.daily_budget       # the service's day: no room once the index was read
        self.store.db.execute("DELETE FROM pickable")
        self.first_pass(w)
        self.assertEqual(len(w.jobs), 0)
        self.assertTrue(w.waits_for_pick(self.alias))

    def test_point_5_never_real_debrid(self):
        rd = {"url": "https://real-debrid.com/d/x", "name": "x", "streamData": {
            "type": "debrid", "filename": "tiny-nosub.mp4", "size": self.size, "service": {"id": "realdebrid", "cached": True}}}

        class RealDebridOnly(Listing):
            def find_row(self, type_, video_id, hints):
                return sources.Aios("").match([rd], hints)
        w = Worker(self.settings(), self.store, RealDebridOnly(None, self.subs()), Safety(), limits=Limits())
        self.workers.append(w)
        self.first_pass(w)
        self.assertEqual((len(w.jobs), self.file_key(), self.srv.log), (0, None, []))   # nothing read, nothing started
        self.assertFalse(w.waits_for_pick(self.alias))

    def test_a_pick_before_the_first_pass_ended_counts_as_picked(self):
        w = self.worker(Limits())
        w.picked_early[self.alias] = time.time()
        self.first_pass(w)
        self.assertEqual(self.queued_audio(w), [(self.alias, True, False)])

    def test_the_audio_job_checks_again_when_it_starts_and_counts_both(self):
        limits = Limits()
        w = self.first_pass(self.worker(limits))
        w._sync_all = lambda *a: None                   # the audio fit itself runs in the image's tests
        job = w.jobs.pop(self.alias)
        w.queue.clear()
        w.process(job)
        self.assertEqual((limits.syncs, limits.opened, limits.started), (1, 1, 1))   # one file, one audio sync, opened
        self.assertGreater(limits.read, 0)
        w.process(job)                                  # the same job again (stopped and queued again): not counted twice
        self.assertEqual((limits.syncs, limits.opened), (1, 1))
        limits.per_day = 1
        again = Job("movie", "tt1", self.hints)
        again.audio = again.opened = True
        w.process(again)                                # a new one past the day's count: nothing read, it waits
        self.assertEqual(limits.syncs, 1)
        self.assertTrue(w.waits_for_pick(self.alias))


class PickedTest(Base):
    choice = "pick"

    def test_a_picked_sync_counts_one_and_checks_the_count(self):
        limits = Limits(per_day=1)
        w = self.first_pass(self.worker(limits))
        self.assertTrue(w.waits_for_pick(self.alias))
        self.assertEqual(len(w.jobs), 0)                 # "when I pick it": nothing starts on the first pass
        w._sync_all = lambda *a: None
        self.assertTrue(w.pick(self.alias))
        job = w.jobs.pop(self.alias)
        w.queue.clear()
        self.assertEqual((job.audio, job.opened), (True, False))
        w.process(job)
        self.assertEqual((limits.syncs, limits.opened), (1, 0))
        with w.cv:
            w._put_waiting(Job("movie", "tt1", self.hints))
        self.assertFalse(w.pick(self.alias))            # today's audio syncs are used up: it waits for another pick
        self.assertTrue(w.waits_for_pick(self.alias))
        self.assertEqual(len(w.jobs), 0)

    def test_an_open_person_can_still_pick(self):
        limits = Limits(opened_per_day=0)
        w = self.first_pass(self.worker(limits, settings=self.settings("open")))
        self.assertTrue(w.waits_for_pick(self.alias))
        self.assertTrue(w.pick(self.alias))
        self.assertEqual(self.queued_audio(w), [(self.alias, True, False)])


class OffTest(Base):
    """A person whose choice is off: no pick entry and no audio job; their files with no track get the "(original)"
    entry and any copy someone made; their first pass doesn't mark the file judged for others."""
    choice = "off"

    def app(self, w):
        from subsync.addon import App
        return App(w.s, self.store, w, Safety())

    def test_the_first_pass_starts_nothing_and_lists_the_original(self):
        w = self.first_pass(self.worker(Limits()))
        self.assertEqual(len(w.jobs), 0)
        self.assertFalse(w.waits_for_pick(self.alias))
        self.assertFalse(w.pick(self.alias))
        self.assertIsNone(self.store.judged_all(self.file_key(), ["eng"], w.s.rule_id, 0))   # others can still pick it
        subs = self.app(w).subtitles("movie", "tt1", f"filename=tiny-nosub.mp4&videoSize={self.size}")
        self.assertEqual([s["label"] for s in subs], ["English (original)"])
        self.assertEqual(len(w.jobs), 0)                 # read lately: not queued again

    def test_no_audio_lane_in_public_mode(self):
        w = self.worker(Limits())
        w.start()
        self.assertIsNone(getattr(w, "audio_worker", None))

    def test_stored_copies_still_listed_and_a_pick_entry_never(self):
        w = self.first_pass(self.worker(Limits(), settings=self.settings(SUBSYNC_LANGUAGES="eng,spa"),
                                        subs=self.subs("eng", "spa")))
        key = self.store.put(self.file_key(), "a" * 64, "eng", 0, "synced", "x", route="audio", body="1\n", fmt="srt",
                             candidate_id="os-eng", original="1\n")
        self.store.set_offers(self.file_key(), "eng", [key])
        self.store.judged_put(self.file_key(), ["eng"], w.s.rule_id)    # someone else's audio sync of English
        with w.cv:   # a file this person waited on before they turned it off (its row is kept 7 days)
            w._put_waiting(Job("movie", "tt1", self.hints))
        subs = self.app(w).subtitles("movie", "tt1", f"filename=tiny-nosub.mp4&videoSize={self.size}")
        self.assertEqual([s["label"] for s in subs], ["English (synced)", "Spanish (original)"])
        self.assertEqual(len(w.jobs), 0)                 # Spanish isn't judged, but reading again can't help them
        self.assertFalse(w.pick(self.alias))
        self.assertEqual(len(w.jobs), 0)

    def test_a_file_read_for_someone_else_gets_no_first_pass_of_theirs(self):
        other = self.first_pass(self.worker(Limits(who="b" * 16), settings=self.settings("pick")))
        self.assertTrue(other.waits_for_pick(self.alias))
        w = self.worker(Limits())
        self.app(w).subtitles("movie", "tt1", f"filename=tiny-nosub.mp4&videoSize={self.size}")
        self.assertEqual(len(w.jobs), 0)
        picker = self.worker(Limits(who="c" * 16), settings=self.settings("pick"))
        self.app(picker).subtitles("movie", "tt1", f"filename=tiny-nosub.mp4&videoSize={self.size}")
        self.assertEqual([(j.alias, j.audio) for j in picker.jobs.values()], [(self.alias, False)])


class SelfHostedTest(Base):
    """Self-hosted "open" (SUBSYNC_AUDIO_WHEN=open, or the old SUBSYNC_AUDIO_ON_PICK=off) takes the same path: an audio
    job on the audio lane, the room checked, counted in the store's day counters."""

    def self_hosted(self, **env):
        return Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://x.test", "SUBSYNC_AUDIO_ROUTE": "on",
                              "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_EXTRA_SOURCES": ""}, **env))

    def test_open_uses_the_audio_lane_the_room_check_and_the_stores_counts(self):
        for env in ({"SUBSYNC_AUDIO_WHEN": "open"}, {"SUBSYNC_AUDIO_ON_PICK": "off"}):
            with self.subTest(env=env):
                self.store.db.execute("DELETE FROM budget")
                w = self.first_pass(self.worker(settings=self.self_hosted(**env)))
                self.assertEqual(self.queued_audio(w), [(self.alias, True, True)])
                self.assertFalse(w.audio_allowed(Job("movie", "tt1", self.hints)))   # the first pass: index only
                w._sync_all = lambda *a: None
                w.process(w.jobs.pop(self.alias))
                w.queue.clear()
                self.assertEqual((self.store.day_count("audio_syncs"), self.store.day_count("audio_opened")), (1, 1))

    def test_no_room_in_the_day_waits_for_a_pick(self):
        w = self.worker(settings=self.self_hosted(SUBSYNC_AUDIO_WHEN="open"))
        w._audio_need = lambda alias: w.s.daily_budget       # the day has no room once the index was read
        self.first_pass(w)
        self.assertEqual(len(w.jobs), 0)
        self.assertTrue(w.waits_for_pick(self.alias))

    def test_the_days_count_reached_waits_for_a_pick(self):
        w = self.worker(settings=self.self_hosted(SUBSYNC_AUDIO_WHEN="open", SUBSYNC_USER_OPENED_PER_DAY="1"))
        self.store.day_add("audio_opened", 1)
        self.first_pass(w)
        self.assertEqual(len(w.jobs), 0)
        self.assertTrue(w.waits_for_pick(self.alias))


# ------------------------------------------------------------------------------------- the queue, picks and endings

class FillsMeanwhile(Limits):
    """A person's limits whose queue is filled by another request of theirs between the answers in `answers` (each
    may_queue call takes the next; then the queue's real answer)."""

    def __init__(self, answers, **kw):
        super().__init__(**kw)
        self.answers = list(answers)

    def may_queue(self, queued):
        return self.answers.pop(0) if self.answers else super().may_queue(queued)


class QueueTest(Base):
    """A pick, a first pass that finds the file waiting and a request that asks for the list again queue a job and change
    the file's state in one step: what they say happened is what happened."""
    choice = "pick"

    def waiting(self, limits):
        w = self.first_pass(self.worker(limits))
        self.assertTrue(w.waits_for_pick(self.alias))
        return w

    def test_a_pick_starts_only_an_audio_job_it_queued(self):
        for answers in ([True, False], [False]):   # the queue fills while the pick runs; full already
            with self.subTest(answers=answers):
                self.store.db.execute("DELETE FROM pickable")
                limits = FillsMeanwhile([], queue=3)
                w = self.waiting(limits)
                limits.answers = list(answers)
                started = w.pick(self.alias)
                self.assertEqual(started, self.alias in w.jobs)
                if not started:   # it waits for another pick, in memory and in the store
                    self.assertTrue(w.waits_for_pick(self.alias))
                    self.assertTrue(self.worker(Limits()).waits_for_pick(self.alias))

    def test_a_first_pass_that_finds_the_pick_queues_its_audio_job_or_leaves_the_file_waiting(self):
        limits = FillsMeanwhile([True, False])
        w = self.worker(limits)
        w.picked_early[self.alias] = time.time()            # the entry was downloaded while the index was read
        self.first_pass(w)
        queued = self.queued_audio(w) == [(self.alias, True, False)]
        self.assertTrue(queued or w.waits_for_pick(self.alias))

    def test_asking_for_the_list_again_says_whether_a_job_is_queued(self):
        w = self.worker(Limits(queue=0))
        w.remember(Job("movie", "tt1", self.hints))
        self.assertFalse(w.ask_list_again(self.alias))      # the person's queue is full: nothing queued
        self.assertEqual(len(w.jobs), 0)
        w = self.worker(Limits())
        w.remember(Job("movie", "tt1", self.hints))
        self.assertTrue(w.ask_list_again(self.alias))
        self.assertEqual(list(w.jobs), [self.alias])

    def test_a_file_the_listing_didnt_have_isnt_listed_again_for_a_pick(self):
        class Counted(Listing):
            calls = 0

            def find_row(self, *a):
                Counted.calls += 1
                return None
        w = Worker(self.settings(), self.store, Counted(None, self.subs()), Safety(), limits=Limits())
        self.workers.append(w)
        w.submit(Job("movie", "tt1", self.hints))
        w.queue.clear()
        w.process(w.jobs.pop(self.alias))                   # not in the listing (uncached): held for UNLISTED_S
        self.assertTrue(w.recently_unlisted(self.alias))
        for _ in range(3):                                  # a pick of its entry, again and again
            self.assertFalse(w.ask_list_again(self.alias))
        self.assertEqual((Counted.calls, len(w.jobs)), (1, 0))

    def test_a_new_job_of_a_file_is_waited_for_not_an_earlier_jobs_placeholders(self):
        w = self.worker(Limits())
        with w.cv:
            w.done[self.alias] = {"eng": None}              # an earlier job of the file published nothing
        w.submit(Job("movie", "tt1", self.hints))           # asked again (another rule, say): queued, not run here
        t0 = time.time()
        self.assertEqual(w.wait_for(self.alias, "eng", 0.3), (None, None))
        self.assertIsNone(w.wait_for_file(self.alias, 0.3))
        self.assertGreaterEqual(time.time() - t0, 0.5)      # both waited for this job's first pass
        w = self.worker(Limits())
        with w.cv:
            w.done[self.alias] = {"eng": "k1"}              # a pass an earlier job published is still served
        w.submit(Job("movie", "tt1", self.hints))
        self.assertEqual(w.published(self.alias, "eng"), "k1")

    def test_a_wait_lasts_its_timeout_when_the_wall_clock_steps_back(self):
        """The waits time out on the monotonic clock, so a step of the wall clock (NTP) never holds a request
        past what it may wait."""
        w = self.worker(Limits())
        w.submit(Job("movie", "tt1", self.hints))           # queued, not run here: every wait runs to its timeout
        looks = []

        def stepping_back():                                # each look at the wall clock is 3 s earlier than the last
            looks.append(1)
            return time.time() - 3 * len(looks)
        waits = {"wait_for": lambda: w.wait_for(self.alias, "eng", 0.2),
                 "wait_for_file": lambda: w.wait_for_file(self.alias, 0.2),
                 "wait_for_originals": lambda: w.wait_for_originals(self.alias, 0.2)}
        try:
            with mock.patch.object(worker_mod, "time", mock.Mock(time=stepping_back)):
                for name, wait in waits.items():
                    with self.subTest(wait=name):
                        t0 = time.monotonic()
                        t = threading.Thread(target=wait, daemon=True)
                        t.start()
                        t.join(2.0)
                        self.assertFalse(t.is_alive())
                        self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            with w.cv:                                      # a wait that outlived its check ends now
                w.jobs.clear()
                w.queue.clear()
                w.cv.notify_all()


class StopsWhileWaiting:
    """An audio place given only once the worker was stopped (Pool.stop while the pick waited for its turn)."""

    def __init__(self, w):
        self.w = w

    def __enter__(self):
        self.w.stop()
        return self

    def __exit__(self, *exc):
        return False


class AudioEndingTest(Base):
    """A picked audio job that ends without its sync, however it ends, leaves its file waiting for another pick (README:
    "if it stops half-way, the file waits for another pick"); a refused debrid file still backs off."""
    choice = "pick"

    def picked(self, w):
        self.first_pass(w)
        self.assertTrue(w.pick(self.alias))
        self.assertFalse(w.waits_for_pick(self.alias))
        return w

    def one_audio_turn(self, w):
        """The audio lane's loop, in this thread, for the one queued job."""
        real = w.process

        def once(job):
            try:
                real(job)
            finally:
                w.stop_flag = True
        w.process = once
        w._loop("audio")

    def test_a_failure_during_the_sync_leaves_the_file_waiting(self):
        for error in (ReadFailed("timed out"), Refused("HTTP 429"), RuntimeError("a bug")):
            with self.subTest(error=type(error).__name__):
                self.store.db.execute("DELETE FROM pickable")
                self.store.db.execute("DELETE FROM files")
                w = self.picked(self.worker(Limits()))

                def failing(*a, error=error):
                    raise error
                w._sync_all = failing                       # the audio fit fails after the index was read
                self.one_audio_turn(w)
                self.assertTrue(w.waits_for_pick(self.alias))
                self.assertTrue(self.worker(Limits()).waits_for_pick(self.alias))   # its row too
                # 1 h, 6 h, 24 h, for this person: a refusal is most likely their account's limit
                self.assertEqual(w._file_blocked(self.alias), isinstance(error, Refused))
                self.assertFalse(self.store.file_blocked(self.alias))   # never for anyone else

    def test_a_pick_queued_when_the_worker_stops_waits_again(self):
        w = self.picked(self.worker(Limits()))
        w.stop()                                            # Pool.stop: a settings change, an idle worker
        self.assertTrue(w.waits_for_pick(self.alias))
        self.assertTrue(self.worker(Limits()).waits_for_pick(self.alias))   # the person's next worker finds it

    def test_a_pick_stopped_while_it_waits_for_its_turn_waits_again(self):
        w = self.picked(self.worker(Limits()))
        w.audio_turn = StopsWhileWaiting(w)
        w._loop("audio")
        self.assertTrue(w.waits_for_pick(self.alias))

    def test_an_audio_job_whose_read_fails_backs_off_and_waits_and_a_pick_meanwhile_reads_nothing(self):
        w = self.picked(self.worker(Limits()))
        w.aios.row = {"url": self.srv.url("gone.mp4")}      # the debrid file is gone (HTTP 404)
        w.queue.clear()
        w.process(w.jobs.pop(self.alias))
        self.assertTrue(self.store.file_blocked(self.alias))
        self.assertTrue(w.waits_for_pick(self.alias))
        self.assertTrue(w.pick(self.alias))
        w.queue.clear()
        log = len(self.srv.log)
        w.process(w.jobs.pop(self.alias))                   # backing off: nothing read, it keeps waiting
        self.assertEqual(len(self.srv.log), log)
        self.assertTrue(w.waits_for_pick(self.alias))


class MapsTest(Base):
    """The worker's in-memory maps: a file asked about again is the newest entry, not the first to go; downloads
    nothing serves any more are dropped; the maps both lanes write are written under the worker's lock
; the person's limits are called, never probed."""
    choice = "pick"

    def test_a_file_asked_about_again_is_the_last_forgotten(self):
        w = self.worker(Limits())
        with mock.patch.object(worker_mod, "SEEN_ENTRIES", 3):
            for name in ("a", "b", "c", "a", "d"):
                w.remember(Job("movie", "tt1", {"filename": f"{name}.mkv", "videoSize": 1}))
        self.assertEqual(list(w.seen), ["name:c.mkv|1", "name:a.mkv|1", "name:d.mkv|1"])

    def downloads(self, w, *names, at=None):
        with w.cv:
            for name in names:
                w.fetched_subs[f"http://subs.test/{name}"] = (time.time() if at is None else at, b"x" * 40)

    def fetch(self, w, name):
        with mock.patch.object(worker_mod, "fetch_subtitle", lambda url, headers=None: b"x" * 40):
            self.assertIsNotNone(w._fetch_one(Job("movie", "tt1", self.hints), "eng", 0,
                                              {"url": f"http://subs.test/{name}", "id": name, "lang": "eng"}))

    def test_downloads_no_one_serves_are_dropped(self):
        w = self.worker(Limits())
        self.downloads(w, "stale", at=time.time() - worker_mod.LIST_TTL_S - 1)
        self.downloads(w, "fresh")
        self.fetch(w, "new")
        self.assertEqual(sorted(w.fetched_subs), ["http://subs.test/fresh", "http://subs.test/new"])

    def test_a_download_fetched_again_is_the_newest(self):
        w = self.worker(Limits())
        with mock.patch.object(worker_mod, "FETCHED_CAP", 100):
            self.downloads(w, "again", at=time.time() - worker_mod.LIST_TTL_S - 1)
            self.downloads(w, "fresh")
            self.fetch(w, "again")                          # stale: downloaded again for the audio job
            self.fetch(w, "new")                            # past the cap: the oldest goes
        self.assertEqual(list(w.fetched_subs), ["http://subs.test/again", "http://subs.test/new"])

    def written_under_the_lock(self, w, call):
        """call() in a thread while this thread holds the worker's lock: True if it had to wait for it."""
        with w.cv:
            t = threading.Thread(target=call, daemon=True)
            t.start()
            t.join(0.3)
            waited = t.is_alive()
        t.join(5)
        return waited

    def test_a_file_the_listing_doesnt_have_is_noted_under_the_lock(self):
        w = Worker(self.settings(), self.store, Listing(None, self.subs()), Safety(), limits=Limits())
        self.workers.append(w)
        self.assertTrue(self.written_under_the_lock(w, lambda: w.process(Job("movie", "tt1", self.hints))))
        self.assertTrue(w.recently_unlisted(self.alias))

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_limits_are_called_not_probed(self):
        from subsync.public import StoreOnly
        w = self.worker(StoreOnly())                        # a person who gets no worker now
        self.assertIsNone(w.who)
        self.assertFalse(w._may_start_audio(False))
        self.assertFalse(w._audio_room(self.alias))

        class Partial:                                      # a limits object without the audio counts or the room
            who = None

            def may_queue(self, queued):
                return True
        w = self.worker(Partial())
        with self.assertRaises(AttributeError):             # an error, never a limit silently off
            w._may_start_audio(False)
        with self.assertRaises(AttributeError):
            w._audio_room(self.alias)


# ---------------------------------------------------------------------------------------------- Usenet in the route

class Redirect:
    """A Usenet service's link: each look-up (a fresh one could fetch the NZB again) redirects to the file."""

    def __init__(self, target):
        self.target, self.hits = target, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                self.send_response(302)
                self.send_header("Location", outer.target)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/playback/1"

    def close(self):
        stop_server(self.server)


class UsenetTest(Base):
    """A Usenet file with no usable track now waits for a pick like a debrid one; the audio
    job reuses its first pass's row and the address it resolved."""
    choice = "pick"

    def setUp(self):
        super().setUp()
        self.link = Redirect(self.srv.url("tiny-nosub.mp4"))
        self.addCleanup(self.link.close)
        self.counts = Counts()
        self.usenet = FakeUsenet([row(url=self.link.url, name="tiny-nosub.mp4", size=self.size)], subs=self.subs())
        self.limits = Limits()
        self.w = Worker(self.settings(), self.store, FakeDebrid(self.subs()), Safety(), limits=self.limits,
                        usenet=self.usenet, usenet_gate=worker_mod.UsenetGate(self.store, 10, self.counts, who="w"))
        self.workers.append(self.w)

    def picked(self):
        self.w.process(Job("movie", "tt1", self.hints))
        self.assertTrue(self.w.waits_for_pick(self.alias))
        self.assertTrue(self.w.pick(self.alias))
        self.w.queue.clear()
        return self.w.jobs.pop(self.alias)

    def test_a_usenet_file_with_no_track_waits_for_a_pick_and_the_pick_reuses_its_row_and_address(self):
        job = self.picked()
        self.assertIsNone(self.store.judged_all(self.file_key(), ["eng"], self.w.s.rule_id, 0))
        self.assertEqual((self.usenet.listings, self.counts.n, self.link.hits), (1, 1, 1))
        read = self.limits.read
        self.w._sync_all = lambda *a: None
        self.w.process(job)
        self.assertTrue(job.usenet and job.audio)
        self.assertGreater(self.limits.read, read)                     # the audio job read the file again
        self.assertEqual((self.usenet.listings, self.counts.n, self.link.hits), (1, 1, 1))       # nothing asked again
        self.assertEqual(self.w._cap(job), self.w.s.file_cap)           # the audio route's cap, not the index cap

    def test_after_the_rows_hour_a_pick_lists_again_only_through_the_gate(self):
        job = self.picked()
        when, kept_row, address = self.w.usenet_rows[self.alias]
        self.w.usenet_rows[self.alias] = (when - worker_mod.USENET_ROW_KEPT_S - 1, kept_row, address)
        self.counts.stopped_ = True                                     # Usenet stopped for the day: nothing listed
        self.w._sync_all = lambda *a: None
        self.w.process(job)
        self.assertEqual(self.usenet.listings, 1)
        self.assertTrue(self.w.waits_for_pick(self.alias))              # it keeps waiting
        self.counts.stopped_ = False
        self.assertTrue(self.w.pick(self.alias))
        self.w.queue.clear()
        self.w.process(self.w.jobs.pop(self.alias))
        self.assertEqual((self.usenet.listings, self.counts.n), (2, 2))  # listed again, counted

    def test_a_read_that_fails_during_the_audio_sync_doesnt_stop_usenet(self):
        job = self.picked()

        def failing(*a):
            raise ReadFailed("short reply")
        self.w._sync_all = failing
        self.w.submit(job)
        self.w.start()
        self.assertTrue(wait_until(lambda: self.w.waits_for_pick(self.alias)))   # it waits for another pick
        self.assertFalse(self.counts.stopped_)
        self.assertFalse(self.store.file_blocked(self.alias))

    def test_the_usenet_rows_are_read_and_written_under_the_lock(self):
        # Both lanes find and keep Usenet rows; a request thread waits for the lock like for every other map
        job = Job("movie", "tt1", self.hints)
        with self.w.cv:
            t = threading.Thread(target=self.w._usenet_row, args=(job, dict(self.hints)), daemon=True)
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive())
        t.join(5)
        self.assertIn(self.alias, self.w.usenet_rows)

    def test_an_index_read_that_fails_in_the_audio_job_doesnt_stop_usenet(self):
        job = self.picked()
        self.link.target = self.srv.url("gone.mp4")
        self.w.usenet_rows[self.alias] = self.w.usenet_rows[self.alias][:2] + (self.srv.url("gone.mp4"),)
        self.w.process(job)
        self.assertFalse(self.counts.stopped_)
        self.assertFalse(self.store.file_blocked(self.alias))
        self.assertTrue(self.w.waits_for_pick(self.alias))
        self.assertLessEqual(self.link.hits, 1 + 1 + worker_mod.USENET_MAX_RE_RESOLVE)   # its link asked at most twice


# --------------------------------------------------------------------------------------------------- the start lines

class SmallTrackTest(unittest.TestCase):
    """A file whose only track is too small for its subtitles (a signs track not flagged forced) waits for a
    pick after its first pass, which marks its languages judged though only the audio route can judge them: on a torrent
    row (or a Usenet one) the pick's audio job finds those marks, and it still reads the file; once the audio route has
    judged it, a later audio job reads nothing."""

    def test_the_picks_audio_job_reads_the_file(self):
        from helpers import FakeDownloads, dialogue_srt
        srv = RangeServer(FIXTURES)
        self.addCleanup(srv.close)
        tmp = TempDir()
        folder = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        url = "http://subs.test/big.srt"
        downloads = FakeDownloads({url: dialogue_srt("eng", n=200)})   # 200 lines; the file's track has 30
        patcher = downloads.patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        row = {"url": srv.url("tiny.mkv"), "streamData": {"torrent": {"infoHash": "a" * 40, "fileIdx": 0}}}
        s = Settings({"SUBSYNC_DATA": folder, "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_LANGUAGES": "eng",
                      "SUBSYNC_EXTRA_SOURCES": ""})
        store = closed_after(self, Store(folder))
        w = Worker(s, store, Listing(row, [{"id": "big", "lang": "eng", "url": url, "release": "Movie"}]), Safety())
        first = Job("movie", "tt1", {"filename": "tiny.mkv", "videoSize": os.path.getsize(os.path.join(FIXTURES,
                                                                                                         "tiny.mkv"))})
        w.process(first)
        file_key = store.resolve(first.alias)
        self.assertTrue(store.file_detail(file_key)["reference"])   # it has a track, too small for the subtitle:
        self.assertTrue(w.waits_for_pick(first.alias))               # it waits for a pick
        self.assertTrue(store.judged_all(file_key, ["eng"], s.rule_id, 0))
        self.assertTrue(w.pick(first.alias))
        synced = []

        def audio_job():
            with w.cv:
                w.queue.clear()
                job = w.jobs.pop(first.alias)
            self.assertTrue(job.audio)
            w.process(job)
            return job
        w._sync_all = lambda job, *a: synced.append(job)
        job = audio_job()
        self.assertEqual(synced, [job])                             # the audio route judged it
        store.put(file_key, "a" * 64, "eng", 0, "keep_original", "x", route="audio", body="1\n", fmt="srt",
                  candidate_id="big", original="1\n")
        with w.cv:
            w._put_waiting(first.again())                           # picked again (another person's pick, say)
        self.assertTrue(w.pick(first.alias))
        audio_job()
        self.assertEqual(len(synced), 1)                            # judged by the audio route: nothing to read


class StartLineTest(unittest.TestCase):
    def public(self, **env):
        return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on"}, **env))

    def test_the_public_line(self):
        from subsync.__main__ import public_start_line
        self.assertIn("2 files at once; audio route allowed: each person off / on pick / when opened, up to 1 GB a file, "
                      "1 at a time, 20 a person a day (5 opened);", public_start_line(self.public(), 2, 1))
        self.assertIn("audio route allowed: each person off / on pick, up to 0.5 GB a file, 3 at a time, 7 a person a "
                      "day;", public_start_line(self.public(SUBSYNC_AUDIO_BUDGET_GB="0.5", SUBSYNC_USER_OPENED_PER_DAY="0",
                                                            SUBSYNC_USER_AUDIO_PER_DAY="7"), 4, 3))
        self.assertIn("; audio route off;", public_start_line(self.public(SUBSYNC_AUDIO_ROUTE="off"), 1, 1))

    def test_the_self_hosted_line(self):
        from subsync.__main__ import start_line
        s = Settings({"SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_LANGUAGES": "eng,rus"})
        self.assertEqual(start_line(s, False), "listening on 8790; languages eng,rus; audio route on pick, up to 1 GB a "
                                               "file, 1 at a time, 20 a day; Usenet off; original language from "
                                               "Wikidata only")
        s = Settings({"SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_AUDIO_WHEN": "open", "SUBSYNC_USENET": "on"})
        self.assertIn("audio route when opened, up to 1 GB a file, 1 at a time, 20 a day (5 opened); Usenet on, 10 "
                      "listings a day", start_line(s, True))
        self.assertIn("; audio route off;", start_line(Settings({}), False))

    def test_the_lines_name_the_original_languages_source(self):
        # By explain.lookup_kind, the privacy page's own decision, in the start lines' words; never the key
        from subsync import explain
        from subsync.__main__ import ORIGINAL_LANGUAGE_WORDS, public_start_line, start_line
        self.assertEqual(set(ORIGINAL_LANGUAGE_WORDS), set(explain.PRIVACY_LOOKUP))
        for env, words in (({"SUBSYNC_ORIGINAL_LANGUAGE": "off", "SUBSYNC_TMDB_KEY": "KEY-1"}, "off"),
                           ({"SUBSYNC_TMDB_KEY": "KEY-1"}, "from TMDB and Wikidata"),
                           ({"SUBSYNC_TMDB_PERSONAL": "on"}, "from Wikidata only")):     # personal keys: public only
            line = start_line(Settings(env), False)
            self.assertTrue(line.endswith(f"; original language {words}"), line)
            self.assertNotIn("KEY-1", line)
        self.assertTrue(public_start_line(self.public(SUBSYNC_TMDB_PERSONAL="on"), 1, 1).endswith(
            "; original language from TMDB with each person's own key, and Wikidata"))
        self.assertTrue(public_start_line(self.public(SUBSYNC_TMDB_KEY="KEY-1", SUBSYNC_TMDB_PERSONAL="on"), 1, 1)
                        .endswith("; original language from TMDB and Wikidata"))


if __name__ == "__main__":
    unittest.main()
