"""Public mode: sign-up, refresh and delete.

Sign-up uses the person's AIOStreams password once, in this request only, to read their config, make a sync config on
their AIOStreams (as ./setup makes one: no Real-Debrid, no Usenet, no AIOSubtitles, nothing that starts work in the
background, so opening a file opens only that file) and, if they leave the box checked, add AIOSubtitles to their own
config. The password is never logged, and stored only when the person ticks the box that lets AIOSubtitles keep it:
then it sits in their encrypted record and the daily check (recheck.py) rebuilds the sync config from their config by
itself; unticking deletes it. The record keeps the sync config's own link and password (made here).

Errors come back as SignupError with a plain message that never repeats the link or the password.
"""
import collections
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from . import explain, realdebrid
from .aiosconfig import (AiosApi, AiosError, is_subsync_preset, new_key, parse_languages, sync_config,
                         sync_config_problems, viewer_change, without_builtin_twins, without_subsync)
from .users import new_token

log = logging.getLogger("subsync.signup")

ADDON_NAME = "AIOSubtitles"
OWN_NAMES = ("aiosubtitles", "subsync")    # left out of the sync config: this service, and a self-hosted subsync
USER_AGENT = "AIOSubtitles/1.0 (public)"
MAX_LINK = 2000
MAX_PASSWORD = 500
HOUR = 3600
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SignupError(Exception):
    """A plain message for the page; `field` names the form field it is about (the page marks it), or None."""

    def __init__(self, message, code=400, field=None):
        super().__init__(message)
        self.message, self.code, self.field = message, code, field


@dataclass
class SignupResult:
    token: str
    manifest_url: str
    changes: list = field(default_factory=list)
    warning: str = None
    added: bool = False
    manual: bool = False
    languages: list = field(default_factory=list)
    store_only: bool = False      # Real-Debrid is their only debrid service: stored copies only
    to_store_only: bool = False   # a refresh that moved them to stored copies only


def public_api(instance, timeout):
    return AiosApi(instance, timeout=timeout, user_agent=USER_AGENT, max_timeout=timeout, retry_wait_cap=5,
                   on_wait=lambda s: log.info("AIOStreams asked to slow down; waiting %d s", s))


# ------------------------------------------------------------------------------------------- what people type

def parse_link(text):
    """(instance address, config id) from an AIOStreams config link (https only)."""
    text = (text or "").strip()
    if not text or len(text) > MAX_LINK or _CONTROL.search(text):
        raise SignupError("Paste the link of your AIOStreams config: the one that ends in /manifest.json.", field="link")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "https":
        raise SignupError("The link must start with https://, so your password crosses the internet encrypted. "
                          "Use the address your AIOStreams has on the internet.", field="link")
    if parts.username is not None or parts.password is not None:
        raise SignupError("Paste the link without a name or password in its address: the one AIOStreams' configure "
                          "page gives.", field="link")
    if parts.query or parts.fragment or not parts.hostname:
        raise SignupError("That isn't an AIOStreams config link: copy it again from AIOStreams' configure page.",
                          field="link")
    prefix, sep, rest = parts.path.partition("/stremio/")
    pieces = rest.split("/")
    if not sep or len(pieces) < 2 or not _UUID.match(pieces[0]) or not pieces[1] or (
            len(pieces) > 2 and pieces[2:] != ["manifest.json"]):
        raise SignupError("That isn't an AIOStreams config link: it should look like "
                          "https://…/stremio/<config id>/…/manifest.json. Copy it again from AIOStreams' configure page.",
                          field="link")
    return f"https://{parts.netloc}{prefix.rstrip('/')}", pieces[0].lower()


def _password(form):
    password = form.get("password") or ""
    if not password or len(password) > MAX_PASSWORD or _CONTROL.search(password):
        raise SignupError("Type your AIOStreams password: the one you set on AIOStreams' configure page.",
                          field="password")
    return password


def _languages(text, limit=4):
    """The chosen languages as codes, at most `limit` (the instance's SUBSYNC_MAX_LANGUAGES); English when none."""
    if not (text or "").strip():
        text = "English"
    langs, problem = parse_languages(text, limit or 4)
    if problem:
        raise SignupError(problem, field="languages")
    return [three for _, _, three in langs]


def form_languages(form):
    """The languages a form chose, as text for _languages. The page's chips send each ticked box (`lang`) and the
    order they were picked in (`lang_order`, kept by the page's script; without it, the order the page showed, and
    boxes ticked since then follow in the page's order). An older form sends typed names (`languages`). None when the
    form has neither."""
    if form.get("lang_ui") != "1":
        return form.get("languages")
    chosen = form.get("lang") or []
    if isinstance(chosen, str):
        chosen = [chosen]
    chosen = [c.strip() for c in chosen if c.strip()]
    if not chosen:
        raise SignupError("Choose at least one language.", field="languages")
    order = [c for c in (form.get("lang_order") or "").split(",") if c in chosen]
    return ", ".join(dict.fromkeys(order + chosen))


WRONG_PASSWORD_CODES = ("USER_INVALID_DETAILS", "USER_INVALID_PASSWORD", "USER_NOT_FOUND")
WRONG_PASSWORD = ("Your AIOStreams didn't accept this password for this link. Type the password of this config: the one "
                  "you set on AIOStreams' configure page.")


def _plain(e, doing, own_password=False):
    """An AIOStreams error in plain words, without the link or the password. own_password: the call used the
    person's own password, so a refused config is about that password."""
    if own_password and str(e.code) in WRONG_PASSWORD_CODES:
        return WRONG_PASSWORD
    if "Can't reach" in e.message:
        return (f"{doing}: AIOSubtitles can't reach your AIOStreams from the internet. Check the link, and that "
                "your AIOStreams can be opened from outside your home network.")
    if e.code in ("RATE_LIMIT_EXCEEDED", 429) or "limiting requests" in e.message:
        return (f"{doing}: your AIOStreams limits how many configs one address can make in an hour, and other people "
                "on the same AIOStreams have signed up lately. Try again in an hour.")
    return e.message if e.message.startswith(doing) else f"{doing}: {e.message}"


class SignupGate:
    """Checked before a sign-up contacts anything: the invite code, attempts per address per hour, the user cap."""

    def __init__(self, settings, count_users, clock=time.time):
        self.s, self.count_users, self.clock = settings, count_users, clock
        self.attempts = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def check(self, ip, invite):
        now = self.clock()
        with self.lock:
            for key in list(self.attempts):
                seen = self.attempts[key]
                while seen and seen[0] <= now - HOUR:
                    seen.popleft()
                if not seen:
                    del self.attempts[key]
            seen = self.attempts[ip]
            if len(seen) >= self.s.signups_per_hour:
                raise SignupError("Too many sign-up attempts from your address in the last hour. Try again in an hour.",
                              429)
            seen.append(now)   # every attempt counts, wrong invite codes included
        if self.s.invite and not hmac.compare_digest(str(invite or "").strip().encode(), self.s.invite.encode()):
            raise SignupError("That invite code isn't right. AIOSubtitles is open only to invited people for now.", 403,
                              field="invite")
        if self.count_users() >= self.s.max_users:
            raise SignupError("AIOSubtitles is full for now. Try again in a few days.", 503)


# ------------------------------------------------------------------------------------------- the steps

def _read_viewer(api, uuid, password):
    try:
        status = api.status()
        viewer, _ = api.get_config(uuid, password)
    except AiosError as e:
        raise SignupError(_plain(e, "Reading your AIOStreams config", own_password=True), field=_field_of(e)) from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SignupError("Your AIOStreams answered in a way AIOSubtitles doesn't know. Is the link from AIOStreams?")
    if not isinstance(viewer, dict) or not isinstance(status, dict):
        raise SignupError("Your AIOStreams answered in a way AIOSubtitles doesn't know. Is the link from AIOStreams?")
    presets = (status.get("settings") or {}).get("presets") if isinstance(status.get("settings"), dict) else None
    metadata = {p["ID"]: p for p in presets or [] if isinstance(p, dict) and "ID" in p}
    return viewer, metadata


def _field_of(e):
    """The field an AIOStreams error is about: the password it refused, or the link it couldn't reach."""
    if str(e.code) in WRONG_PASSWORD_CODES:
        return "password"
    if "Can't reach" in e.message or "not like AIOStreams" in e.message:
        return "link"
    return None


def store_only(record):
    """A record of a setup whose only debrid service is Real-Debrid: no sync config, nothing read."""
    return record.get("store_only") is True


def _to_store_only(record):
    """The record's sync config fields, taken out of it as they were (None when it had none): the record is now for
    stored copies only, and the caller removes that sync config once the record is saved."""
    fields = (record.get("sync_uuid"), record.get("sync_link"), record.get("sync_password"))
    for name in ("sync_uuid", "sync_link", "sync_password", "sync_fp", "synced_at"):
        record.pop(name, None)
    record["store_only"] = True
    return fields if fields[0] else None


def _sync_for(viewer, manifest_url):
    """The sync config for a viewer config that has a debrid service AIOSubtitles may use (Real-Debrid-only setups
    are found first, with realdebrid.real_debrid_only, and get none)."""
    if realdebrid.real_debrid_only(viewer):
        raise SignupError(realdebrid.REAL_DEBRID_ONLY)
    warning = explain.PUBLIC_REAL_DEBRID_WARNING if realdebrid.SERVICE in realdebrid.debrid_in_use(viewer) else None
    sync = sync_config(viewer, manifest_url, names=OWN_NAMES)
    problems = sync_config_problems(sync)
    if problems:
        raise SignupError("AIOSubtitles can't find any file it could read through your AIOStreams: "
                          + "; ".join(problems) + ".")
    return sync, warning


def _add_addon(api, uuid, password, viewer, manifest_url, metadata, clean_up=False):
    """(changes, added, manual): AIOSubtitles placed in the person's own config and read back; with `clean_up`
 the subtitle addons it asks itself are taken out of that config."""
    new, changes = viewer_change(viewer, manifest_url, metadata, name=ADDON_NAME, clean_up=clean_up)
    if not changes:
        return [], True, False
    try:
        api.save_config(uuid, password, new)
        saved, _ = api.get_config(uuid, password)
        added = any(is_subsync_preset(p, manifest_url, ()) for p in (saved or {}).get("presets") or [])
    except (AiosError, KeyError, TypeError, ValueError, AttributeError):
        added = False
    return (changes if added else []), added, not added


def sync_fingerprint(sync):
    """A short fingerprint of a sync config as AIOSubtitles last made it: the daily rebuild saves only when it differs."""
    return hashlib.sha256(json.dumps(sync, sort_keys=True).encode()).hexdigest()[:32]


def _new_sync(api, instance, sync):
    """A new sync config on `instance`, checked by its manifest: (id, link, password). Nothing is left behind when the
    check fails."""
    sync_password = new_key(24)
    try:
        sync_uuid, sync_encrypted = api.create_config(sync, sync_password)
    except AiosError as e:
        raise SignupError(_plain(e, "Making AIOSubtitles' config on your AIOStreams")) from None
    except (KeyError, TypeError, ValueError):
        raise SignupError("Your AIOStreams answered in a way AIOSubtitles doesn't know when making its config.")
    try:
        api.addon_get(f"/stremio/{sync_uuid}/{sync_encrypted}", "/manifest.json")
    except (AiosError, KeyError, TypeError, ValueError) as e:
        try:   # don't leave a config behind that nothing uses
            api.delete_config(sync_uuid, sync_password)
        except AiosError:
            pass
        message = _plain(e, "Checking AIOSubtitles' config on your AIOStreams") if isinstance(e, AiosError) else \
            "Your AIOStreams answered in a way AIOSubtitles doesn't know when checking its config."
        raise SignupError(message) from None
    return sync_uuid, f"{instance}/stremio/{sync_uuid}/{sync_encrypted}/manifest.json", sync_password


def _put_sync(api, record, sync, gone=False):
    """`sync` saved into the record's sync config, or into a new one when that one is gone (`gone`: known already, so
    not tried): (id, link, password)."""
    if not gone:
        try:
            api.save_config(record["sync_uuid"], record["sync_password"], sync)
            return record["sync_uuid"], record["sync_link"], record["sync_password"]
        except AiosError as e:
            if str(e.code) not in WRONG_PASSWORD_CODES:
                raise SignupError(_plain(e, "Updating AIOSubtitles' config on your AIOStreams")) from None
    return _new_sync(api, record["instance"], sync)


def drop_sync(api, fields):
    """Removes a sync config this service made and won't use (best effort): (id, link, password)."""
    try:
        api.delete_config(fields[0], fields[2])
    except AiosError:
        pass


def all_clear(settings, record, now):
    """A record after a sign-up, a refresh or a rebuild: nothing to report, the next check a period from now."""
    record.update(state=None, unreachable=0, checked_at=now, next_check=now + settings.recheck_hours * HOUR)
    record.pop("state_detail", None)
    record.pop("main_gone_since", None)


def _synced(settings, record, sync, fields, now):
    record["sync_uuid"], record["sync_link"], record["sync_password"] = fields
    record.update(sync_fp=sync_fingerprint(sync), synced_at=now)
    all_clear(settings, record, now)


STORED = "stored copies only"   # rebuild's answer when a config became Real-Debrid-only


def rebuild(settings, record, token, api, password, force=False):
    """With the password a person let AIOSubtitles keep: AIOSubtitles' config made again from theirs, saved only when
    it changed (or `force`: when it is gone). Returns the new sync config, or None when nothing changed; the record's
    sync fields are updated. Both ways: a stored-copies-only record whose config now has another debrid
    service gets a sync config; a config that became Real-Debrid-only returns STORED, the record's sync fields taken
    out (the caller removes that sync config once the record is saved, and finds its fields in record["dropped"]).
    Raises SignupError (field "password" when the password is refused). Their own config is never changed."""
    viewer, _ = _read_viewer(api, record["main_uuid"], password)
    if realdebrid.real_debrid_only(viewer):
        if store_only(record):
            return None
        record["dropped"] = _to_store_only(record)
        return STORED
    sync, _ = _sync_for(viewer, f"{settings.public_base}/u/{token}/manifest.json")
    if store_only(record):
        fields = _new_sync(api, record["instance"], sync)
        record.pop("store_only", None)
    elif not force and sync_fingerprint(sync) == record.get("sync_fp"):
        return None
    else:
        fields = _put_sync(api, record, sync, gone=force)
    record["sync_uuid"], record["sync_link"], record["sync_password"] = fields
    record["sync_fp"] = sync_fingerprint(sync)
    return sync


def signup(settings, users, form, api_factory=public_api, timeout=30):
    langs = _languages(form.get("languages"), settings.max_languages)
    instance, uuid = parse_link(form.get("link"))
    password = _password(form)
    api = api_factory(instance, timeout)
    viewer, metadata = _read_viewer(api, uuid, password)
    token = new_token()
    manifest_url = f"{settings.public_base}/u/{token}/manifest.json"
    # The clean-up goes with adding AIOSubtitles (alone it would leave fewer subtitles); AIOSubtitles' own config
    # then lacks those addons too, so it asks their sites itself
    clean_up = form.get("add_addon") == "on" and form.get("clean_up") == "on"
    record = {"instance": instance, "main_uuid": uuid, "languages": langs, "version": 1}
    # with Real-Debrid as the only debrid service, stored copies only: no sync config, nothing of theirs read;
    # the clean-up is left out, since AIOSubtitles then asks no subtitle site for them
    stored = realdebrid.real_debrid_only(viewer)
    if stored:
        clean_up, warning, sync_uuid = False, None, None
        _to_store_only(record)
        all_clear(settings, record, users.clock())
    else:
        sync, warning = _sync_for(without_builtin_twins(viewer)[0] if clean_up else viewer, manifest_url)
        sync_uuid, sync_link, sync_password = _new_sync(api, instance, sync)
        _synced(settings, record, sync, (sync_uuid, sync_link, sync_password), users.clock())
    if form.get("keep_password") == "on":    # the person chose it (unticked by default): AIOSubtitles refreshes by itself
        record["auto_password"] = password
    try:   # the record first: the person's own config gets the link only once the link works
        users.create(token, record)
    except Exception:
        if sync_uuid is not None:
            try:
                api.delete_config(sync_uuid, sync_password)
            except AiosError:
                pass
        raise
    changes, added, manual = [], False, False
    if form.get("add_addon") == "on":
        changes, added, manual = _add_addon(api, uuid, password, viewer, manifest_url, metadata, clean_up)
    log.info("signed up: %d language%s%s", len(langs), "" if len(langs) == 1 else "s",
             ", stored copies only" if stored else ", Real-Debrid left out" if warning else "")
    return SignupResult(token, manifest_url, changes, warning, added, manual, langs, store_only=stored)


def refresh(settings, users, token, form, api_factory=public_api, timeout=30):
    """Rebuilds the sync config from the person's config as it is now (and puts AIOSubtitles back if it is missing).
    A gone sync config is made again. With a new config link (`link`), that config becomes theirs; on another
    AIOStreams the sync config moves there and the old one is removed. The kept password follows the box
    (`keep_password`): ticked keeps this password, unticked deletes a kept one."""
    with users.locked(token):
        record = users.open(token)
        if record is None:
            raise SignupError("This link isn't signed up (any more). Sign up again on the start page.")
        link = (form.get("link") or "").strip()
        instance, main_uuid = parse_link(link) if link else (record["instance"], record["main_uuid"])
        password = _password(form)
        languages = (_languages(form.get("languages"), settings.max_languages)
                     if (form.get("languages") or "").strip() else None)
        api = api_factory(instance, timeout)
        viewer, metadata = _read_viewer(api, main_uuid, password)
        manifest_url = f"{settings.public_base}/u/{token}/manifest.json"
        was_stored = store_only(record)
        stored = realdebrid.real_debrid_only(viewer)       # stored copies, both ways at Refresh
        old = sync = warning = fields = None
        made = False
        if stored:
            if not was_stored:                             # the sync config they had goes once the record is saved
                old = (record["instance"], record["sync_uuid"], record["sync_password"])
        else:
            sync, warning = _sync_for(viewer, manifest_url)
            if was_stored:
                fields = _new_sync(api, instance, sync)
            elif instance != record["instance"]:
                fields = _new_sync(api, instance, sync)
                old = (record["instance"], record["sync_uuid"], record["sync_password"])
            else:
                fields = _put_sync(api, record, sync)
            made = fields[0] != record.get("sync_uuid")
        try:
            record["instance"], record["main_uuid"] = instance, main_uuid
            changes, added, manual = [], False, False
            if form.get("add_addon") == "on":
                changes, added, manual = _add_addon(api, main_uuid, password, viewer, manifest_url, metadata, False)
            if languages:
                record["languages"] = languages
            if stored:
                _to_store_only(record)
                all_clear(settings, record, users.clock())
            else:
                record.pop("store_only", None)
                _synced(settings, record, sync, fields, users.clock())
            if form.get("keep_password") == "on":
                record["auto_password"] = password
            else:
                record.pop("auto_password", None)
            users.update(token, record)
        except Exception:
            if made:           # a config made here and not recorded: removed, so nothing holds their keys unused
                drop_sync(api, fields)
            raise
    if old is not None:   # the sync config they no longer use (another AIOStreams, or stored copies only): removed
        try:
            api_factory(old[0], timeout).delete_config(old[1], old[2])
        except AiosError:
            pass
    log.info("refreshed a sync config%s%s", ", stored copies only" if stored else
             ", Real-Debrid left out" if warning else "", ", on another AIOStreams" if old and not stored else "")
    return SignupResult(token, manifest_url, changes, warning, added, manual, record["languages"], store_only=stored,
                        to_store_only=stored and not was_stored)


def forget_password(users, token):
    """Deletes a kept AIOStreams password at once (the settings page's button); nothing else changes."""
    with users.locked(token):
        record = users.open(token)
        if record is None:
            raise SignupError("This link isn't signed up (any more). Sign up again on the start page.")
        if record.pop("auto_password", None) is not None:
            users.update(token, record)
    log.info("a kept password was deleted")


def set_languages(users, token, text, limit=4):
    """Saves a person's languages, at most `limit` (the instance's SUBSYNC_MAX_LANGUAGES). A record holding more from
    before a lowered limit keeps them until this save."""
    with users.locked(token):
        record = users.open(token)
        if record is None:
            raise SignupError("This link isn't signed up (any more). Sign up again on the start page.")
        record["languages"] = _languages(text, limit)
        users.update(token, record)
    return record["languages"]


def delete(settings, users, token, form, api_factory=public_api, timeout=30):
    """Removes the sync config from the person's AIOStreams, with their password also AIOSubtitles from their own
    config, then their record and usage."""
    record = users.open(token)
    if record is None:
        raise SignupError("This link isn't signed up (any more): there is nothing to delete.")
    api = api_factory(record["instance"], timeout)
    outcome = {"sync_removed": False, "addon_removed": False}
    viewer = password = None
    if form.get("password"):   # checked before anything is deleted, so a mistyped password can be typed again
        password = _password(form)
        try:
            viewer, _ = api.get_config(record["main_uuid"], password)
        except AiosError as e:
            raise SignupError(_plain(e, "Reading your AIOStreams config", own_password=True) + " Nothing was deleted.",
                              field=_field_of(e)) from None
        if not isinstance(viewer, dict):
            raise SignupError("Your AIOStreams answered in a way AIOSubtitles doesn't know. Nothing was deleted.")
    if store_only(record):       # no sync config was ever made for them
        outcome["sync_removed"] = True
    else:
        try:
            api.delete_config(record["sync_uuid"], record["sync_password"])
            outcome["sync_removed"] = True
        except AiosError:
            pass
    if viewer is not None:
        manifest_url = f"{settings.public_base}/u/{token}/manifest.json"
        try:
            new, removed = without_subsync(viewer, manifest_url, names=(ADDON_NAME.lower(),))
            if removed:
                api.save_config(record["main_uuid"], password, new)
            outcome["addon_removed"] = True
        except (AiosError, KeyError, TypeError, ValueError, AttributeError):
            pass
    users.delete(token)
    log.info("deleted a record (sync config removed: %s, addon removed: %s)", outcome["sync_removed"],
             outcome["addon_removed"])
    return outcome
