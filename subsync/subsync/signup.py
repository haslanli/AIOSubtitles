"""Public mode: sign-up, refresh and delete.

Sign-up uses the person's AIOStreams password once, in this request only, to read their config, make a sync config on
their AIOStreams (as ./setup makes one: no Real-Debrid, no Usenet, no AIOSubtitles, nothing that starts work in the
background, so opening a file opens only that file), with Usenet on a second one, the Usenet config (only their Usenet
searches and services), and, if they leave the box checked, add AIOSubtitles to their own config. A setup
with no debrid service AIOSubtitles may read gets no sync config: with Usenet on its Usenet config also carries its
subtitle sources, else, with Real-Debrid as its only service, it gets stored copies only. The password is never logged, and stored only when the person ticks the box that lets AIOSubtitles keep it:
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
                         sync_config_problems, usenet_route, usenet_sync_config, viewer_change, without_builtin_twins,
                         without_subsync)
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
    usenet: bool = False          # a Usenet config was made or kept
    usenet_why: str = None        # Usenet was asked for but no config made: "proxy", "nntp" or "none"
    sync_made: bool = True        # a sync config was made or kept (not for stored copies only, nor Usenet only)


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


SYNC_FIELDS = ("sync_uuid", "sync_link", "sync_password", "sync_fp", "synced_at")


def store_only(record):
    """A record of a setup whose only debrid service is Real-Debrid: no sync config, nothing read."""
    return record.get("store_only") is True


def _to_store_only(record):
    """The record's sync config fields, taken out of it as they were (None when it had none): the record is now for
    stored copies only, and the caller removes that sync config once the record is saved."""
    fields = (record.get("sync_uuid"), record.get("sync_link"), record.get("sync_password"))
    for name in SYNC_FIELDS:
        record.pop(name, None)
    record["store_only"] = True
    return fields if fields[0] else None


def wants_usenet(settings, form):
    """The person's Usenet box, ticked on an instance that lets people turn Usenet on."""
    return bool(settings.usenet and settings.usenet_listings_per_day > 0 and form.get("usenet") == "on")


def _has_sync(viewer, manifest_url):
    """Whether the viewer's config has a debrid service AIOSubtitles may read (a sync config can be made for it)."""
    return not realdebrid.real_debrid_only(viewer) and \
        not sync_config_problems(sync_config(viewer, manifest_url, names=OWN_NAMES))


def _sync_for(viewer, manifest_url, usenet=False):
    """The sync config for a viewer config that has a debrid service AIOSubtitles may use (Real-Debrid-only setups
    are found first, with realdebrid.real_debrid_only, and get none). `usenet`: this instance reads Usenet files too, so
    the reason a config can't be used names both."""
    if realdebrid.real_debrid_only(viewer):
        raise SignupError(realdebrid.REAL_DEBRID_ONLY)
    warning = explain.PUBLIC_REAL_DEBRID_WARNING if realdebrid.SERVICE in realdebrid.debrid_in_use(viewer) else None
    sync = sync_config(viewer, manifest_url, names=OWN_NAMES)
    problems = sync_config_problems(sync, usenet=usenet)
    if problems:
        raise SignupError("AIOSubtitles can't find any file it could read through your AIOStreams: "
                          + "; ".join(problems) + ".")
    return sync, warning


def _usenet_for(viewer, manifest_url, subtitles):
    """(Usenet config, the services it reads through, None), or (None, [], why) when the viewer's config has no
    Usenet route (aiosconfig.usenet_route). `subtitles`: it also carries the subtitle sources (no sync config)."""
    services, why = usenet_route(viewer, manifest_url, names=OWN_NAMES)
    if why:
        return None, [], why
    return usenet_sync_config(viewer, manifest_url, names=OWN_NAMES, subtitles=subtitles), services, None


USENET_FIELDS = ("usenet_uuid", "usenet_link", "usenet_password", "usenet_fp", "usenet_subtitles")


def _usenet_state(record, want, services, why):
    """The person's Usenet box, and what came of it: the services read through, or why there is no Usenet config."""
    record["usenet"] = bool(want)
    for name, value in (("usenet_why", why if want else None), ("usenet_services", services if want else None)):
        if value:
            record[name] = value
        else:
            record.pop(name, None)


def _usenet_synced(record, usenet, fields, subtitles):
    record["usenet_uuid"], record["usenet_link"], record["usenet_password"] = fields
    record["usenet_fp"] = sync_fingerprint(usenet)
    record["usenet_subtitles"] = bool(subtitles)


def _drop_usenet(record):
    """The record's Usenet config fields, taken out of it (None when it had none); the caller removes that config once
    the record is saved."""
    fields = (record.get("usenet_uuid"), record.get("usenet_link"), record.get("usenet_password"))
    for name in USENET_FIELDS:
        record.pop(name, None)
    return fields if fields[0] else None


def _drop_sync_fields(record):
    """The record's sync config fields, taken out of it (None when it had none), for a setup that now reads through its
    Usenet config only."""
    fields = (record.get("sync_uuid"), record.get("sync_link"), record.get("sync_password"))
    for name in SYNC_FIELDS:
        record.pop(name, None)
    return fields if fields[0] else None


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


def _put_sync(api, record, sync, gone=False, kind="sync"):
    """`sync` saved into the record's sync config (`kind` "usenet": its Usenet config), or into a new one when that one
    is gone (`gone`: known already, so not tried): (id, link, password)."""
    if not gone:
        try:
            api.save_config(record[f"{kind}_uuid"], record[f"{kind}_password"], sync)
            return record[f"{kind}_uuid"], record[f"{kind}_link"], record[f"{kind}_password"]
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


def _usable_now(settings, record):
    """Whether this record's Usenet box counts now: ticked, and this instance still lets people turn Usenet on."""
    return bool(record.get("usenet")) and settings.usenet and settings.usenet_listings_per_day > 0


def rebuild(settings, record, token, api, password, force=False, force_usenet=False):
    """With the password a person let AIOSubtitles keep: AIOSubtitles' configs made again from theirs, each saved only
    when it changed (or `force`, `force_usenet`: when it is gone). Returns True when anything changed, None when
    nothing did; the record's fields are updated, and configs it no longer uses are listed in record["drop"] for the
    caller to remove once the record is saved. Both ways: a stored-copies-only record whose config now has
    another debrid service (or, with Usenet on, a Usenet route) gets its configs; a config that became Real-Debrid-only
    without a Usenet route returns STORED, the record's sync fields taken out (found in record["dropped"]). The Usenet
    config follows the person's box. Raises SignupError (field "password" when the password is refused).
    Their own config is never changed."""
    viewer, _ = _read_viewer(api, record["main_uuid"], password)
    manifest_url = f"{settings.public_base}/u/{token}/manifest.json"
    want = _usable_now(settings, record)
    has_sync = _has_sync(viewer, manifest_url)
    usenet, services, why = _usenet_for(viewer, manifest_url, not has_sync) if want else (None, [], None)
    drop, changed, made = [], False, None
    if not has_sync and usenet is None:
        if not realdebrid.real_debrid_only(viewer):
            _sync_for(viewer, manifest_url, usenet=settings.usenet)     # raises: no file it could read
        gone = _drop_usenet(record)
        record["drop"] = [gone] if gone else []
        _usenet_state(record, bool(record.get("usenet")), services, why)   # their box stays as they left it
        if store_only(record):
            return True if gone else None
        record["dropped"] = _to_store_only(record)
        return STORED
    # the Usenet config first: when it can't be made, nothing of the sync config's has changed yet
    if usenet is None:
        gone = _drop_usenet(record)
        if gone:
            drop.append(gone)
            changed = True
    elif force_usenet or not record.get("usenet_uuid") or sync_fingerprint(usenet) != record.get("usenet_fp"):
        fields = _put_sync(api, record, usenet, gone=force_usenet or not record.get("usenet_uuid"), kind="usenet")
        if fields[0] != record.get("usenet_uuid"):
            made = fields          # removed again if the sync config can't be made below
        _usenet_synced(record, usenet, fields, not has_sync)
        changed = True
    before = (record.get("usenet_why"), record.get("usenet_services"))
    _usenet_state(record, bool(record.get("usenet")), services, why)
    changed = changed or before != (record.get("usenet_why"), record.get("usenet_services"))
    if not has_sync:                      # Usenet only: no sync config any more
        gone = _drop_sync_fields(record)
        if gone:
            drop.append(gone)
            changed = True
        if store_only(record):
            record.pop("store_only", None)
            changed = True
        record["drop"] = drop
        return True if changed else None
    try:
        sync, _ = _sync_for(viewer, manifest_url)
        if store_only(record) or not record.get("sync_uuid"):
            fields = _new_sync(api, record["instance"], sync)
            record.pop("store_only", None)
        elif not force and sync_fingerprint(sync) == record.get("sync_fp"):
            record["drop"] = drop
            return True if changed else None
        else:
            fields = _put_sync(api, record, sync, gone=force)
    except SignupError:
        if made:                  # nothing left behind: the Usenet config made a moment ago goes too
            drop_sync(api, made)
        raise
    record["sync_uuid"], record["sync_link"], record["sync_password"] = fields
    record["sync_fp"] = sync_fingerprint(sync)
    record["drop"] = drop
    return True


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
    want = wants_usenet(settings, form)
    record = {"instance": instance, "main_uuid": uuid, "languages": langs, "version": 1}
    base = without_builtin_twins(viewer)[0] if clean_up else viewer
    has_sync = _has_sync(base, manifest_url)
    usenet, services, why = _usenet_for(base, manifest_url, not has_sync) if want else (None, [], None)
    # with Real-Debrid as the only debrid service and no Usenet config, stored copies only: no sync config,
    # nothing of theirs read; the clean-up is left out, since AIOSubtitles then asks no subtitle site for them
    stored = realdebrid.real_debrid_only(viewer) and usenet is None
    warning, sync_fields, usenet_fields = None, None, None
    if stored:
        clean_up = False
        _to_store_only(record)
        all_clear(settings, record, users.clock())
    elif has_sync:
        sync, warning = _sync_for(base, manifest_url)
        sync_fields = _new_sync(api, instance, sync)
        _synced(settings, record, sync, sync_fields, users.clock())
    elif usenet is None:
        _sync_for(viewer, manifest_url, usenet=settings.usenet)     # raises: nothing it could read
    else:                                                           # Usenet only
        if realdebrid.SERVICE in realdebrid.debrid_in_use(viewer):
            warning = explain.PUBLIC_REAL_DEBRID_WARNING
        all_clear(settings, record, users.clock())
    if usenet is not None:
        try:
            usenet_fields = _new_sync(api, instance, usenet)
        except SignupError:
            if sync_fields:              # nothing left behind: the sync config made a moment ago goes too
                drop_sync(api, sync_fields)
            raise
        _usenet_synced(record, usenet, usenet_fields, not has_sync)
    _usenet_state(record, want, services, why)
    if form.get("keep_password") == "on":    # the person chose it (unticked by default): AIOSubtitles refreshes by itself
        record["auto_password"] = password
    try:   # the record first: the person's own config gets the link only once the link works
        users.create(token, record)
    except Exception:
        for fields in (sync_fields, usenet_fields):
            if fields:
                drop_sync(api, fields)
        raise
    changes, added, manual = [], False, False
    if form.get("add_addon") == "on":
        changes, added, manual = _add_addon(api, uuid, password, viewer, manifest_url, metadata, clean_up)
    log.info("signed up: %d language%s%s%s", len(langs), "" if len(langs) == 1 else "s",
             ", stored copies only" if stored else ", Real-Debrid left out" if warning else "",
             ", Usenet on" if usenet is not None else "")
    return SignupResult(token, manifest_url, changes, warning, added, manual, langs, store_only=stored,
                        usenet=usenet is not None, usenet_why=why if want else None, sync_made=has_sync and not stored)


def refresh(settings, users, token, form, api_factory=public_api, timeout=30):
    """Rebuilds the sync config, and with the Usenet box ticked the Usenet config, from the person's config as it is now
    (and puts AIOSubtitles back if it is missing). A gone config is made again. With a new config link (`link`), that
    config becomes theirs; on another AIOStreams their configs move there and the old ones are removed. The kept
    password follows the box (`keep_password`): ticked keeps this password, unticked deletes a kept one. Unticking the
    Usenet box removes the Usenet config."""
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
        moved = instance != record["instance"]
        want = wants_usenet(settings, form)
        was_stored = store_only(record)
        has_sync = _has_sync(viewer, manifest_url)
        usenet, services, why = _usenet_for(viewer, manifest_url, not has_sync) if want else (None, [], None)
        stored = realdebrid.real_debrid_only(viewer) and usenet is None   # stored copies, both ways
        if not stored and not has_sync and usenet is None:
            _sync_for(viewer, manifest_url, usenet=settings.usenet)      # raises: nothing it could read
        old = []                  # configs they no longer use, removed once the record is saved
        made = []                 # configs made here, removed again if the record can't be saved
        sync = fields = warning = None
        try:
            if has_sync:
                sync, warning = _sync_for(viewer, manifest_url)
                if was_stored or moved or not record.get("sync_uuid"):
                    fields = _new_sync(api, instance, sync)
                else:
                    fields = _put_sync(api, record, sync)
                if fields[0] != record.get("sync_uuid"):
                    made.append(fields)
            elif usenet is not None and realdebrid.SERVICE in realdebrid.debrid_in_use(viewer):
                warning = explain.PUBLIC_REAL_DEBRID_WARNING
            usenet_fields = None
            if usenet is not None:
                if moved or not record.get("usenet_uuid"):
                    usenet_fields = _new_sync(api, instance, usenet)
                else:
                    usenet_fields = _put_sync(api, record, usenet, kind="usenet")
                if usenet_fields[0] != record.get("usenet_uuid"):
                    made.append(usenet_fields)
        except SignupError:
            for gone in made:
                drop_sync(api, gone)
            raise
        if record.get("sync_uuid") and (not has_sync or moved):
            old.append((record["instance"], record["sync_uuid"], record["sync_password"]))
        if record.get("usenet_uuid") and (usenet is None or moved):
            old.append((record["instance"], record["usenet_uuid"], record["usenet_password"]))
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
            elif has_sync:
                record.pop("store_only", None)
                _synced(settings, record, sync, fields, users.clock())
            else:
                record.pop("store_only", None)
                _drop_sync_fields(record)
                all_clear(settings, record, users.clock())
            if usenet is not None:
                _usenet_synced(record, usenet, usenet_fields, not has_sync)
            else:
                _drop_usenet(record)
            _usenet_state(record, want, services, why)
            if form.get("keep_password") == "on":
                record["auto_password"] = password
            else:
                record.pop("auto_password", None)
            users.update(token, record)
        except Exception:
            for gone in made:  # a config made here and not recorded: removed, so nothing holds their keys unused
                drop_sync(api, gone)
            raise
    for where, uuid, sync_password in old:   # the configs they no longer use: removed
        try:
            api_factory(where, timeout).delete_config(uuid, sync_password)
        except AiosError:
            pass
    log.info("refreshed a sync config%s%s%s", ", stored copies only" if stored else
             ", Real-Debrid left out" if warning else "", ", on another AIOStreams" if moved and not stored else "",
             ", Usenet on" if usenet is not None else "")
    return SignupResult(token, manifest_url, changes, warning, added, manual, record["languages"], store_only=stored,
                        to_store_only=stored and not was_stored, usenet=usenet is not None,
                        usenet_why=why if want else None, sync_made=has_sync and not stored)


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
    """Removes the sync config and the Usenet config from the person's AIOStreams, with their password also AIOSubtitles
    from their own config, then their record and usage."""
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
    removed = True
    for kind in ("sync", "usenet"):          # no sync config for stored copies only or Usenet only
        if record.get(f"{kind}_uuid"):
            try:
                api.delete_config(record[f"{kind}_uuid"], record[f"{kind}_password"])
            except AiosError:
                removed = False
    outcome["sync_removed"] = removed
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


