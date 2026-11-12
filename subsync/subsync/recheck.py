"""Public mode: each person's AIOStreams checked again, at most once per SUBSYNC_RECHECK_HOURS, while they use the service.

A record opens only with the person's personal link, so a check runs when one of their requests comes in, in the
background, never holding up the answer. Without their password AIOStreams tells only whether a config exists, so the
check asks two things: does their own config still exist (no password), and AIOSubtitles' own config, with the password
AIOSubtitles made for it. It repairs what it can by itself (a sync config whose link changed) and records what only
the person can fix, for their settings page and one "action needed" entry in their subtitle lists. A person who ticked
the box that lets AIOSubtitles keep their password also gets AIOSubtitles' config rebuilt from theirs, saved only when it
changed, and made again when it is gone; Real-Debrid and Usenet stay out each time, and their own config is never
changed. One check at a time per AIOStreams address, so an AIOStreams many people share gets at most a few calls at once.
"""
import logging
import threading
import time

from .aiosconfig import AiosError
from .signup import WRONG_PASSWORD_CODES, SignupError, all_clear, drop_sync, public_api, rebuild

log = logging.getLogger("subsync.recheck")
HOUR = 3600
CONFIRM_GONE_S = HOUR        # their own config found gone is looked at again this much later before it counts
UNREACHABLE_CHECKS = 3       # checks in a row that couldn't reach their AIOStreams before the settings page says so


class Checker:
    def __init__(self, settings, users, on_change=None, api_factory=public_api, clock=time.time, timeout=30):
        self.s, self.users, self.on_change = settings, users, on_change
        self.api_factory, self.clock, self.timeout = api_factory, clock, timeout
        self.lock = threading.Lock()
        self.next = {}           # user id -> when their next check is due (read from their record once)
        self.running = set()     # user ids being checked now
        self.instances = {}      # AIOStreams address -> its lock: one check at a time per AIOStreams
        self.threads = []

    # -- starting -------------------------------------------------------------------------------------------------
    def maybe_start(self, token):
        """On a person's request: starts their check in the background when it is due. Never waits."""
        if self.s.recheck_hours <= 0 or not isinstance(token, str):
            return False
        uid = self.users.user_id(token)
        with self.lock:
            due = self.next.get(uid)
            if uid in self.running:
                return False
        if due is None:
            record = self.users.open(token)
            if record is None:
                return False
            due = record.get("next_check") or 0
            with self.lock:
                self.next[uid] = due
        if self.clock() < due:
            return False
        with self.lock:
            if uid in self.running:
                return False
            self.running.add(uid)
            thread = threading.Thread(target=self._run, args=(token, uid), daemon=True, name="subsync-recheck")
            self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        thread.start()
        return True

    def forget(self, token):
        """After a refresh or a delete: the next check's time is read from the record again."""
        with self.lock:
            self.next.pop(self.users.user_id(token), None)

    def join(self, timeout=None):
        """Waits for the checks running now (tests, shutdown)."""
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout)

    def _run(self, token, uid):
        try:
            self.check(token)
        except Exception as e:   # never let a check take anything down
            log.warning("a check failed: %s", type(e).__name__)
        finally:
            with self.lock:
                self.running.discard(uid)

    # -- the check ------------------------------------------------------------------------------------------------
    def check(self, token):
        """One check, now: returns the person's state afterwards (None: nothing to report)."""
        record = self.users.open(token)
        if record is None:
            return None
        found = self._look(token, record)
        changed = False
        with self.users.locked(token):
            current = self.users.open(token)
            if current is None:
                return None
            if any(current.get(k) != record.get(k) for k in ("instance", "main_uuid", "sync_uuid")):
                state = current.get("state")   # a refresh came in meanwhile: what this check saw is about the old configs
                fields = found.get("fields")
                if fields and fields[0] != record["sync_uuid"]:   # a config this check made: not used, so removed
                    drop_sync(self.api_factory(record["instance"], self.timeout), fields)
            else:
                before = (current.get("state"), current.get("sync_link"), current.get("sync_uuid"))
                self._apply(current, found)
                state = current.get("state")
                changed = (state, current.get("sync_link"), current.get("sync_uuid")) != before
                self.users.update(token, current)
            with self.lock:
                self.next[self.users.user_id(token)] = current.get("next_check") or 0
        log.info("checked a person's AIOStreams: %s", state or "all well")
        if changed and self.on_change is not None:
            self.on_change(token)   # their worker starts again with the new record
        return state

    def _instance(self, instance):
        with self.lock:
            return self.instances.setdefault(instance, threading.Lock())

    def _look(self, token, record):
        """What the person's AIOStreams says now; nothing is written to the record here."""
        found = {"sync": None, "main": None, "rebuilt": None, "problem": None}
        api = self.api_factory(record["instance"], self.timeout)
        with self._instance(record["instance"]):
            try:
                _, encrypted = api.get_config(record["sync_uuid"], record["sync_password"])
                found["sync"], found["encrypted"], found["checked"] = "ok", encrypted, record["sync_uuid"]
            except AiosError as e:
                found["sync"] = "gone" if str(e.code) in WRONG_PASSWORD_CODES else \
                    "unreachable" if "Can't reach" in e.message else "error"
            if found["sync"] == "unreachable":
                return found
            try:
                found["main"] = api.config_exists(record["main_uuid"])
            except AiosError:
                found["main"] = None
            password = record.get("auto_password")
            if password and found["main"] is not False and found["sync"] in ("ok", "gone"):
                copy = dict(record)
                try:
                    found["rebuilt"] = rebuild(self.s, copy, token, api, password, force=found["sync"] == "gone")
                    found["fields"] = (copy["sync_uuid"], copy["sync_link"], copy["sync_password"], copy["sync_fp"])
                except SignupError as e:
                    found["problem"] = ("password" if e.field == "password" else
                                        "unreachable" if e.field == "link" else "rebuild", e.message)
        return found

    def _apply(self, record, found):
        now = self.clock()
        record["checked_at"] = now
        record["next_check"] = now + self.s.recheck_hours * HOUR
        if found["sync"] == "unreachable" or (found["problem"] or ("",))[0] == "unreachable":
            record["unreachable"] = (record.get("unreachable") or 0) + 1
            if record["unreachable"] >= UNREACHABLE_CHECKS and not record.get("state"):
                record["state"] = "unreachable"
            return
        record["unreachable"] = 0
        if record.get("state") == "unreachable":
            record["state"] = None
        # their own config: gone only when seen gone twice, an hour apart (a slip of their AIOStreams is not a move)
        if found["main"] is False:
            since = record.get("main_gone_since")
            if since is None:
                record["main_gone_since"] = now
                record["next_check"] = now + CONFIRM_GONE_S
            elif now - since >= CONFIRM_GONE_S:
                record["state"] = "main_gone"
            if found["sync"] == "gone" and record.get("state") != "main_gone":
                record["state"] = "sync_gone"
            self._repair_link(record, found)
            return
        record.pop("main_gone_since", None)
        if record.get("state") == "main_gone" and found["main"] is True:
            record["state"] = None
        kind, detail = found["problem"] or (None, None)
        if kind == "password":   # the kept password no longer opens their config: it is deleted, Refresh asks again
            record.pop("auto_password", None)
            record["state"] = "password_refused"
            return
        if kind == "rebuild":    # their config can no longer be used (only Real-Debrid, no readable source)
            record["state"], record["state_detail"] = "cannot_rebuild", detail
            return
        if found.get("fields"):        # rebuilt from their config with the kept password (or nothing to change)
            if found["rebuilt"] is not None:
                record["sync_uuid"], record["sync_link"], record["sync_password"], record["sync_fp"] = found["fields"]
                record["synced_at"] = now
            all_clear(self.s, record, now)
        elif found["sync"] == "gone":
            record["state"] = "sync_gone"
            return
        if self._repair_link(record, found) and record.get("state") in ("sync_gone", "password_refused",
                                                                        "cannot_rebuild"):
            record["state"] = None
            record.pop("state_detail", None)

    @staticmethod
    def _repair_link(record, found):
        """A sync config that opened: its link as its AIOStreams gives it now. True when it opened."""
        if found["sync"] != "ok" or found.get("checked") != record["sync_uuid"]:
            return False
        link = f"{record['instance']}/stremio/{record['sync_uuid']}/{found['encrypted']}/manifest.json"
        if found["encrypted"] and link != record["sync_link"]:
            record["sync_link"] = link       # their AIOStreams changed the link: repaired, nothing for them to do
        return True
