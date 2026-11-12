"""Public mode: sign-up, refresh and delete.

Sign-up uses the person's AIOStreams password once, in this request only, to read their config, make a sync config on
their AIOStreams (as ./setup makes one: no Real-Debrid, no Usenet, no AIOSubtitles, nothing that starts work in the
background, so opening a file opens only that file) and, if they leave the box checked, add AIOSubtitles to their own
config. The password is never stored or logged; the record keeps the sync config's own link and password (made here).

Errors come back as SignupError with a plain message that never repeats the link or the password.
"""
import collections
import hmac
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from . import explain, realdebrid
from .aiosconfig import (AiosApi, AiosError, is_subsync_preset, new_key, parse_languages, subtitle_sources,
                         sync_config, sync_config_problems, viewer_change, without_subsync)
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


def _languages(text):
    if not (text or "").strip():
        text = "English"
    langs, problem = parse_languages(text)
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


def _sync_for(viewer, manifest_url):
    if realdebrid.real_debrid_only(viewer):
        raise SignupError(realdebrid.REAL_DEBRID_ONLY)
    warning = explain.PUBLIC_REAL_DEBRID_WARNING if realdebrid.SERVICE in realdebrid.debrid_in_use(viewer) else None
    sync = sync_config(viewer, manifest_url, names=OWN_NAMES)
    problems = sync_config_problems(sync)
    if problems:
        raise SignupError("AIOSubtitles can't find any file it could read through your AIOStreams: "
                          + "; ".join(problems) + ".")
    return sync, warning


def _add_addon(api, uuid, password, viewer, manifest_url, metadata, add_opensubtitles):
    """(changes, added, manual): AIOSubtitles placed in the person's own config and read back."""
    offer = add_opensubtitles and not subtitle_sources(viewer, manifest_url)
    new, changes = viewer_change(viewer, manifest_url, metadata, offer, name=ADDON_NAME)
    if not changes:
        return [], True, False
    try:
        api.save_config(uuid, password, new)
        saved, _ = api.get_config(uuid, password)
        added = any(is_subsync_preset(p, manifest_url, ()) for p in (saved or {}).get("presets") or [])
    except (AiosError, KeyError, TypeError, ValueError, AttributeError):
        added = False
    return (changes if added else []), added, not added


def signup(settings, users, form, api_factory=public_api, timeout=30):
    langs = _languages(form.get("languages"))
    instance, uuid = parse_link(form.get("link"))
    password = _password(form)
    api = api_factory(instance, timeout)
    viewer, metadata = _read_viewer(api, uuid, password)
    token = new_token()
    manifest_url = f"{settings.public_base}/u/{token}/manifest.json"
    sync, warning = _sync_for(viewer, manifest_url)
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
    try:   # the record first: the person's own config gets the link only once the link works
        users.create(token, {"instance": instance, "main_uuid": uuid, "sync_uuid": sync_uuid,
                             "sync_link": f"{instance}/stremio/{sync_uuid}/{sync_encrypted}/manifest.json",
                             "sync_password": sync_password, "languages": langs, "version": 1})
    except Exception:
        try:
            api.delete_config(sync_uuid, sync_password)
        except AiosError:
            pass
        raise
    changes, added, manual = [], False, False
    if form.get("add_addon") == "on":
        changes, added, manual = _add_addon(api, uuid, password, viewer, manifest_url, metadata,
                                            form.get("add_opensubtitles") == "on")
    log.info("signed up: %d language%s%s", len(langs), "" if len(langs) == 1 else "s",
             ", Real-Debrid left out" if warning else "")
    return SignupResult(token, manifest_url, changes, warning, added, manual, langs)


def refresh(settings, users, token, form, api_factory=public_api, timeout=30):
    """Rebuilds the sync config from the person's config as it is now (and puts AIOSubtitles back if it is missing)."""
    record = users.open(token)
    if record is None:
        raise SignupError("This link isn't signed up (any more). Sign up again on the start page.")
    password = _password(form)
    api = api_factory(record["instance"], timeout)
    viewer, metadata = _read_viewer(api, record["main_uuid"], password)
    manifest_url = f"{settings.public_base}/u/{token}/manifest.json"
    sync, warning = _sync_for(viewer, manifest_url)
    try:
        api.save_config(record["sync_uuid"], record["sync_password"], sync)
    except AiosError as e:
        raise SignupError(_plain(e, "Updating AIOSubtitles' config on your AIOStreams")) from None
    changes, added, manual = [], False, False
    if form.get("add_addon") == "on":
        changes, added, manual = _add_addon(api, record["main_uuid"], password, viewer, manifest_url, metadata, False)
    if (form.get("languages") or "").strip():
        record["languages"] = _languages(form.get("languages"))
    users.update(token, record)
    log.info("refreshed a sync config%s", ", Real-Debrid left out" if warning else "")
    return SignupResult(token, manifest_url, changes, warning, added, manual, record["languages"])


def set_languages(users, token, text):
    record = users.open(token)
    if record is None:
        raise SignupError("This link isn't signed up (any more). Sign up again on the start page.")
    record["languages"] = _languages(text)
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
