"""Public mode: each person's AIOStreams checked again, at most once per SUBSYNC_RECHECK_HOURS, while they use the service.

A record opens only with the person's personal link, so a check runs when one of their requests comes in, in the
background, never holding up the answer. Without their password AIOStreams tells only whether a config exists, so the
check asks two things: does their own config still exist (no password), and AIOSubtitles' own config, with the password
AIOSubtitles made for it. It repairs what it can by itself (a sync config whose link changed) and records what only
the person can fix, for their settings page and one "action needed" entry in their subtitle lists. A person who ticked
the box that lets AIOSubtitles keep their password also gets AIOSubtitles' config rebuilt from theirs, saved only when it
changed, and made again when it is gone; Real-Debrid and Usenet stay out each time, and their own config is never
changed. One check at a time per AIOStreams address, so an AIOStreams many people share gets at most a few calls at once.
A record for stored copies only (Real-Debrid is their only debrid service) has no sync config to check: it is
checked only when the person let AIOSubtitles keep their password, and the rebuild then moves them both ways.
A person with Usenet on also has a Usenet config: it is looked at, repaired and rebuilt the same way, and a
record with only a Usenet config (no debrid service) has no sync config to look at. Nothing is listed or read.
"""
import logging
import threading
import time

from .aiosconfig import PUBLIC_TIMEOUT, UNREACHABLE, AiosError, install_link
from .signup import (STORED, SYNC_FIELDS, USENET_FIELDS, WRONG_PASSWORD_CODES, SignupError, all_clear, config_fields,
                     drop_sync, instance_key, public_api, rebuild, store_only)

KINDS = ("sync", "usenet")   # AIOSubtitles' configs on a person's AIOStreams: the sync config and the Usenet config
# the record fields a rebuild may change (signup.rebuild), copied from its working copy into the record (_apply sets
# synced_at itself)
REBUILT = SYNC_FIELDS + USENET_FIELDS + ("usenet", "usenet_why", "usenet_services", "store_only")
# the record fields naming the configs a check looks at: a refresh that changed any of them while the check ran makes
# what it saw old news
LOOKED_AT = ("instance", "main_uuid", "sync_uuid", "usenet_uuid", "store_only")

log = logging.getLogger("subsync.recheck")
HOUR = 3600
CONFIRM_GONE_S = HOUR        # their own config found gone is looked at again this much later before it counts
UNREACHABLE_CHECKS = 3       # checks in a row that couldn't reach their AIOStreams (or, got an error answer
                             # from it) before the settings page says so


def _problem(e):
    """What a rebuild's SignupError says, as (kind, detail): "password" (the kept password is refused), "unreachable"
    (their AIOStreams wasn't reached), "refused" (it refused the config AIOSubtitles made: its reason), "rebuild" (their
    config can't be used as it is), else "error" (an error answer: busy, a fault, an answer AIOSubtitles can't read)."""
    if e.field == "password":
        return "password", e.message
    if e.field == "link":
        return "unreachable", e.message
    if e.refused is not None:
        return "refused", e.refused
    return ("rebuild" if e.unusable else "error"), e.message


def _due(record):
    """When the record's next check is due: never for stored copies only without a kept password (nothing to check
    until a refresh, which forgets this), else its next_check (0: now)."""
    if store_only(record) and not record.get("auto_password"):
        return float("inf")
    return record.get("next_check") or 0


def _snapshot(record):
    """What the person's worker runs on, of what a check may change: their state and each of AIOSubtitles' configs'
    link and id. A check that changes it starts their worker again (on_change)."""
    return (record.get("state"),) + tuple(record.get(f"{kind}_{name}") for kind in KINDS for name in ("link", "uuid"))


class Checker:
    def __init__(self, settings, users, on_change=None, api_factory=public_api, clock=time.time,
                 timeout=PUBLIC_TIMEOUT):
        self.s, self.users, self.on_change = settings, users, on_change
        self.api_factory, self.clock, self.timeout = api_factory, clock, timeout
        self.lock = threading.Lock()
        self.next = {}           # user id -> when their next check is due (_due: read from their record once, then
                                 # set by each check)
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
            due = _due(record)
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
            with self.lock:      # tried again a period later, like a check that ran, not at their next request
                self.next[uid] = max(self.next.get(uid, 0), self.clock() + self.s.recheck_hours * HOUR)
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
        changed, drop = False, []
        with self.users.locked(token):
            current = self.users.open(token)
            if current is None or any(current.get(k) != record.get(k) for k in LOOKED_AT):
                # deleted, or a refresh came in meanwhile: what this check saw is about the old configs
                state = (current or {}).get("state")
                fields = found.get("fields") or {}
                for kind in KINDS:             # made by this check, not used: removed
                    made = config_fields(fields, kind)
                    if made[0] and made[0] != record.get(f"{kind}_uuid"):
                        drop.append(made)
            else:
                before = _snapshot(current)
                self._apply(current, found)
                state = current.get("state")
                changed = _snapshot(current) != before
                self.users.update(token, current)
                drop = list(found.get("drop") or [])
                if store_only(current):   # the configs of a config that became Real-Debrid-only, once the record says so
                    drop += [d for d in found.get("dropped") or [] if d]
            if current is not None:
                with self.lock:
                    self.next[self.users.user_id(token)] = _due(current)
        for fields in drop:   # nothing refers to them any more: removed without holding up the person's own actions
            drop_sync(self.api_factory(record["instance"], self.timeout), fields)
        if current is None:
            return None
        log.info("checked a person's AIOStreams: %s", state or "all well")
        if changed and self.on_change is not None:
            self.on_change(token)   # their worker starts again with the new record
        return state

    def _instance(self, instance):
        with self.lock:   # one lock per AIOStreams, however a record spells its address
            return self.instances.setdefault(instance_key(instance), threading.Lock())

    def _look(self, token, record):
        """What the person's AIOStreams says now; nothing is written to the record here."""
        found = {"sync": None, "usenet": None, "main": None, "rebuilt": None, "problem": None}
        api = self.api_factory(record["instance"], self.timeout)
        stored = store_only(record)
        with self._instance(record["instance"]):
            for kind in KINDS:      # stored copies only, or Usenet only: no sync config of theirs to look at
                if not record.get(f"{kind}_uuid"):
                    continue
                try:
                    _, encrypted = api.get_config(record[f"{kind}_uuid"], record[f"{kind}_password"])
                    found[kind], found[f"{kind}_encrypted"] = "ok", encrypted
                    found[f"{kind}_checked"] = record[f"{kind}_uuid"]
                except AiosError as e:
                    found[kind] = "gone" if str(e.code) in WRONG_PASSWORD_CODES else \
                        "unreachable" if e.code == UNREACHABLE else "error"     # by its code, never its words
                if found[kind] == "unreachable":
                    return found
            try:
                found["main"] = api.config_exists(record["main_uuid"])
            except AiosError:
                found["main"] = None
            password = record.get("auto_password")
            looked = [found[k] for k in KINDS if record.get(f"{k}_uuid")]
            if password and found["main"] is not False and (stored or all(s in ("ok", "gone") for s in looked)):
                copy = dict(record)
                try:
                    rebuilt = rebuild(self.s, copy, token, api, password, force=found["sync"] == "gone",
                                      force_usenet=found["usenet"] == "gone")
                    if rebuilt is STORED:          # their config became Real-Debrid-only
                        found["to_store_only"] = True
                        found["dropped"] = [copy.get("dropped")] + list(copy.get("drop") or [])
                    else:
                        found["rebuilt"] = rebuilt
                        found["fields"] = {k: copy.get(k) for k in REBUILT}
                        found["drop"] = list(copy.get("drop") or [])
                except SignupError as e:
                    found["problem"] = _problem(e)
        return found

    def _apply(self, record, found):
        now = self.clock()
        record["checked_at"] = now
        record["next_check"] = now + self.s.recheck_hours * HOUR
        kind, detail = found["problem"] or (None, None)
        if "unreachable" in (found["sync"], found["usenet"]) or kind == "unreachable":
            record["unreachable"] = (record.get("unreachable") or 0) + 1
            record.pop("errors", None)
            if record["unreachable"] >= UNREACHABLE_CHECKS and not record.get("state"):
                record["state"] = "unreachable"
            return
        record["unreachable"] = 0
        if record.get("state") == "unreachable":
            record["state"] = None
        if kind != "error":
            record.pop("errors", None)
            if record.get("state") == "aiostreams_errors":
                record["state"] = None
        gone = "gone" in (found["sync"], found["usenet"])
        # their own config: gone only when seen gone twice, an hour apart (a slip of their AIOStreams is not a move)
        if found["main"] is False:
            since = record.get("main_gone_since")
            if since is None:
                record["main_gone_since"] = now
                record["next_check"] = now + CONFIRM_GONE_S
            elif now - since >= CONFIRM_GONE_S:
                record["state"] = "main_gone"
            if gone and record.get("state") != "main_gone":
                record["state"] = "sync_gone"
            self._repair_links(record, found)
            return
        record.pop("main_gone_since", None)
        if record.get("state") == "main_gone" and found["main"] is True:
            record["state"] = None
        if kind is not None:     # the rebuild failed: AIOSubtitles keeps its configs, each at the link that opens it
            self._repair_links(record, found)
        if kind == "error":      # an error answer passes; counted as "couldn't reach" is, never action needed
            record["errors"] = (record.get("errors") or 0) + 1
            if record["errors"] >= UNREACHABLE_CHECKS and not record.get("state"):
                record["state"] = "aiostreams_errors"
            return
        if kind == "password":   # the kept password no longer opens their config: it is deleted, Refresh asks again
            record.pop("auto_password", None)
            record["state"] = "password_refused"
            return
        if kind == "rebuild":    # their config can no longer be used (only Real-Debrid, no readable source)
            record["state"], record["state_detail"] = "cannot_rebuild", detail if detail.endswith(".") else detail + "."
            return
        if kind == "refused":    # AIOStreams refused AIOSubtitles' config, a fault of AIOSubtitles
            # one line for the operator when the state becomes so: AIOStreams' reason alone, nothing about the person,
            # their link or a key
            if record.get("state") != "config_refused":
                log.warning('AIOStreams refused the config AIOSubtitles made at a daily check: "%s"', detail)
            record["state"], record["state_detail"] = "config_refused", detail
            return
        if found.get("to_store_only"):   # stored copies only from now on; check() removes the old configs
            for name in SYNC_FIELDS + USENET_FIELDS + ("usenet_services",):
                record.pop(name, None)
            record["store_only"] = True
            all_clear(self.s, record, now)
            return
        if found.get("fields") is not None:   # rebuilt from their config with the kept password (or nothing to change)
            if found["rebuilt"] is not None:
                for name, value in found["fields"].items():
                    if value is None:
                        record.pop(name, None)
                    else:
                        record[name] = value
                record["synced_at"] = now
            all_clear(self.s, record, now)
        elif gone:
            record["state"] = "sync_gone"
            return
        if self._repair_links(record, found) and record.get("state") in ("sync_gone", "password_refused",
                                                                         "cannot_rebuild"):
            record["state"] = None
            record.pop("state_detail", None)

    @staticmethod
    def _repair_links(record, found):
        """The configs that opened: each one's link as its AIOStreams gives it now. True when every config the record
        has opened."""
        opened = True
        for kind in KINDS:
            if not record.get(f"{kind}_uuid"):
                continue
            if found[kind] != "ok" or found.get(f"{kind}_checked") != record.get(f"{kind}_uuid"):
                opened = False
                continue
            link = install_link(record["instance"], record[f"{kind}_uuid"], found[f"{kind}_encrypted"])
            if found[f"{kind}_encrypted"] and link != record[f"{kind}_link"]:
                record[f"{kind}_link"] = link    # their AIOStreams changed the link: repaired, nothing for them to do
        return opened and any(record.get(f"{k}_uuid") for k in KINDS)
