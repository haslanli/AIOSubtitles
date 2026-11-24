"""Public mode: sign-up, refresh and delete.

Sign-up uses the person's AIOStreams password once, in this request only, to read their config, make a sync config on
their AIOStreams (as ./setup makes one: no Real-Debrid, no Usenet, no AIOSubtitles, nothing that starts work in the
background, so opening a file opens only that file), with Usenet on a second one, the Usenet config (only their Usenet
searches and services), and, if they leave the box checked, add AIOSubtitles to their own config. A setup
with no debrid service AIOSubtitles may read gets no sync config: with Usenet on its Usenet config also carries its
subtitle sources, else, with Real-Debrid as its only service, it gets stored copies only. The password is never
logged, and stored only when the person ticks the box that lets AIOSubtitles keep it: then it sits in their encrypted
record and the daily check (recheck.py) rebuilds the sync config from their config by itself; unticking deletes it. The
record keeps the sync config's own link and password (made here).

Errors come back as SignupError with a plain message that never repeats the link or the password.
"""
import collections
import contextlib
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
from .aiosconfig import (ALIAS_NO_CONFIG, ALIAS_UNKNOWN, BAD_ANSWER, CODE_WORD, MAX_ALIAS, MAX_LANGUAGES,
                         NOT_AIOSTREAMS, PUBLIC_RETRY_WAIT_CAP, PUBLIC_TIMEOUT, TOO_SLOW, UNREACHABLE, UNREADABLE,
                         UUID_RE, WRONG_PASSWORD_CODES, AiosApi, AiosError, install_link, is_limited, is_subsync_preset,
                         needs_sign_in, new_key, parse_languages, sync_config, sync_config_problems, usenet_route,
                         usenet_sync_config, viewer_change, without_builtin_twins, without_subsync)
from .extra import cleanable_presets
from .langs import DEFAULT_LANGUAGE
from .langs import name as lang_name
from .settings import AUDIO_CHOICES
from .users import new_token
from .version import user_agent

log = logging.getLogger("subsync.signup")

ADDON_NAME = "AIOSubtitles"
OWN_NAMES = ("aiosubtitles", "subsync")    # left out of the sync config: this service, and a self-hosted subsync
USER_AGENT = user_agent("public")
MAX_LINK = 2000
MAX_PASSWORD = 500
HOUR = 3600
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SignupError(Exception):
    """A plain message for the page; `field` names the form field it is about (the page marks it), or None.
    `refused`: AIOStreams' own reason when it refused a config AIOSubtitles made (sign-up and Refresh then say so
    plainly, _refusal_explained). `unusable`: the person's config itself can't be used as it is (no file AIOSubtitles
    could read through it), which only they can change; the daily check tells it from an error answer."""

    def __init__(self, message, code=400, field=None, refused=None, unusable=False):
        super().__init__(message)
        self.message, self.code, self.field, self.refused, self.unusable = message, code, field, refused, unusable


@dataclass
class SignupResult:
    token: str
    manifest_url: str
    changes: list = field(default_factory=list)
    warning: tuple = None         # (title, text): explain.PUBLIC_REAL_DEBRID_WARNING when Real-Debrid was left out
    added: bool = False
    manual: bool = False          # adding AIOSubtitles failed and nothing in their config changed: added by hand
    unchecked: bool = False       # its change was sent but couldn't be checked: `changes` were sent, not made
    languages: list = field(default_factory=list)
    store_only: bool = False      # Real-Debrid is their only debrid service: stored copies only
    to_store_only: bool = False   # a refresh that moved them to stored copies only
    usenet: bool = False          # a Usenet config was made or kept
    usenet_why: str = None        # Usenet was asked for but no config made: "proxy", "nntp" or "none"
    sync_made: bool = True        # a sync config was made or kept (not for stored copies only, nor Usenet only)


def public_api(instance, timeout):
    return AiosApi(instance, timeout=timeout, user_agent=USER_AGENT, max_timeout=timeout,
                   retry_wait_cap=PUBLIC_RETRY_WAIT_CAP,
                   on_wait=lambda s: log.info("AIOStreams asked to slow down; waiting %d s", s))


def personal_manifest_url(settings, token):
    """A person's personal link: their own AIOSubtitles' manifest (public.USER_PATH reads it back). The one place its
    shape is written, since AIOSubtitles' entry in their config is found by it (_add_addon, delete)."""
    return f"{settings.public_base}/u/{token}/manifest.json"


# ------------------------------------------------------------------------------------------- what people type

UNKNOWN_ALIAS = ("Your AIOStreams doesn't know this alias link: its alias may have been changed or removed. Copy the "
                 "install link again from AIOStreams' configure page.")
NO_CONFIG_FROM_ALIAS = ("AIOSubtitles couldn't get your config's full link from this alias link. Open the alias link in "
                        "your browser: AIOStreams takes you to the full link, which has /stremio/ and a long code in it. "
                        "Copy that address and paste it here.")
REFUSED_CONFIG = ("Your AIOStreams didn't accept the config AIOSubtitles makes from yours. This is a fault in "
                  "AIOSubtitles, not in your config, and your own config wasn't changed. AIOStreams' reason: \"{}\"")
NOT_A_LINK = ("That isn't an AIOStreams config link: it should look like https://…/stremio/<config id>/…/manifest.json. "
              "Copy it again from AIOStreams' configure page.")
NOT_SIGNED_UP = "This link isn't signed up (any more). Sign up again on the start page."
UNKNOWN_ANSWER = "Your AIOStreams answered in a way AIOSubtitles doesn't know"   # each step ends the sentence
ALIAS_STEP = "Opening your alias link"


def _variant_query(query):
    """A query of only a variant selection (?v=<ids>, AIOStreams 2.33+)."""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return len(pairs) == 1 and pairs[0][0] == "v" and bool(pairs[0][1].strip())


def _install_tail(tail, bare=False):
    """What may follow the config part of an install link: manifest.json, after a variant selection (v/<ids>) or not;
    `bare`: nothing at all too (a full link without manifest.json, taken as before)."""
    return tail == ["manifest.json"] or (bare and not tail) or (
        len(tail) == 3 and tail[0] == "v" and tail[1] and tail[2] == "manifest.json")


def _address(parts):
    """https://<host>[:<port>] of a link, one spelling for each AIOStreams: the host in lower case without a trailing
    dot, the port only when it isn't https' own (443). A port that isn't one stays as typed, so instance_key never fails
    on an address (parse_link refuses such links)."""
    host = parts.hostname.rstrip(".") or parts.hostname
    if ":" in host:                     # an IPv6 address
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = parts.netloc.rpartition(":")[2]
    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def instance_key(instance):
    """An AIOStreams address as parse_link spells it (a record from before keeps it as it was typed), so two addresses
    of one AIOStreams compare equal: whether a Refresh moves a person, and the daily check's one-at-a-time lock."""
    parts = urllib.parse.urlsplit(instance)
    return _address(parts) + parts.path.rstrip("/") if parts.hostname else instance


def parse_link(text):
    """(instance address, config id, None) from an AIOStreams install link, or (instance address, None, alias) from an
    alias link (…/stremio/u/<alias>/…, looked up by _config_id); https only. Either may carry a variant selection
    (…/v/<ids>/manifest.json or ?v=<ids>), which AIOSubtitles doesn't use: it reads the config itself."""
    text = (text or "").strip()
    if not text or len(text) > MAX_LINK or _CONTROL.search(text):
        raise SignupError("Paste the link of your AIOStreams config: the one that ends in /manifest.json.", field="link")
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port              # read now: a port that isn't one (":abc", ":99999") raises ValueError too
    except ValueError:          # that, or an unmatched "[": the link error, before anything is asked
        raise SignupError(NOT_A_LINK, field="link") from None
    if parts.scheme != "https":
        raise SignupError("The link must start with https://, so your password crosses the internet encrypted. "
                          "Use the address your AIOStreams has on the internet.", field="link")
    if parts.username is not None or parts.password is not None:
        raise SignupError("Paste the link without a name or password in its address: the one AIOStreams' configure "
                          "page gives.", field="link")
    if (parts.query and not _variant_query(parts.query)) or parts.fragment or not parts.hostname:
        raise SignupError("That isn't an AIOStreams config link: copy it again from AIOStreams' configure page.",
                          field="link")
    prefix, sep, rest = parts.path.partition("/stremio/")
    pieces = rest.split("/")
    instance = _address(parts) + prefix.rstrip("/")
    if sep and len(pieces) >= 3 and pieces[0] == "u" and pieces[1] and _install_tail(pieces[2:]):
        alias = urllib.parse.unquote(pieces[1])
        if len(alias) > MAX_ALIAS or _CONTROL.search(alias):
            raise SignupError(UNKNOWN_ALIAS, field="link")
        return instance, None, alias
    if (not sep or len(pieces) < 2 or not UUID_RE.fullmatch(pieces[0]) or not pieces[1]
            or not _install_tail(pieces[2:], True)):
        raise SignupError(NOT_A_LINK, field="link")
    return instance, pieces[0].lower(), None


def _config_id(api, uuid, alias):
    """The config id of a pasted link: as it was given, or, for an alias link, looked up on the person's AIOStreams
    (one request, no password, the redirect not followed)."""
    if alias is None:
        return uuid
    try:
        return api.alias_config_id(alias)
    except AiosError as e:
        if e.code == ALIAS_UNKNOWN:
            raise SignupError(UNKNOWN_ALIAS, field="link") from None
        if e.code == ALIAS_NO_CONFIG:
            raise SignupError(NO_CONFIG_FROM_ALIAS, field="link") from None
        if isinstance(e.code, int):   # its server error (500 to 599): the AIOStreams fails for now, not the link
            raise SignupError(f"{ALIAS_STEP}: your AIOStreams answered with an error (HTTP {e.code}). Try again in a "
                              "few minutes.", field="link") from None
        # no box is marked for an answer too slow to come: AIOStreams was reached at that link
        raise SignupError(_plain(e, ALIAS_STEP), field=None if e.code == TOO_SLOW else "link") from None


@contextlib.contextmanager
def _refusal_explained():
    """Sign-up and Refresh: a config of AIOSubtitles' that AIOStreams refused is AIOSubtitles' fault, and said so
. The daily rebuild keeps its own words for its notice."""
    try:
        yield
    except SignupError as e:
        if e.refused is None:
            raise
        raise SignupError(REFUSED_CONFIG.format(e.refused)) from None


def _refused(e):
    """AIOStreams' reason when it refused a config (USER_INVALID_CONFIG), else None."""
    return e.detail if str(e.code) == "USER_INVALID_CONFIG" and e.detail else None


def _password(form):
    password = form.get("password") or ""
    if not password or len(password) > MAX_PASSWORD or _CONTROL.search(password):
        raise SignupError("Type your AIOStreams password: the one you set on AIOStreams' configure page.",
                          field="password")
    return password


def _languages(text, limit=MAX_LANGUAGES):
    """The chosen languages as codes, at most `limit` (the instance's SUBSYNC_MAX_LANGUAGES); DEFAULT_LANGUAGE (English)
    when none."""
    if not (text or "").strip():
        text = lang_name(DEFAULT_LANGUAGE)
    langs, problem = parse_languages(text, limit or MAX_LANGUAGES)
    if problem:
        raise SignupError(problem, field="languages")
    return [three for _, _, three in langs]


def form_languages(form):
    """The languages a form chose, as text for _languages. The pages' language slots (ui.language_slots, marked by
    `lang_ui`) send each slot's choice (`lang`, empty when unused) in the page's order, which is the order kept
. An older form sends typed names (`languages`). None when the form has neither."""
    if form.get("lang_ui") != "1":
        return form.get("languages")
    chosen = form.get("lang") or []
    if isinstance(chosen, str):
        chosen = [chosen]
    chosen = [c.strip() for c in chosen if c.strip()]
    if not chosen:
        raise SignupError("Choose at least one language.", field="languages")
    return ", ".join(dict.fromkeys(chosen))


WRONG_PASSWORD = ("Your AIOStreams didn't accept this password for this link. Type the password of this config: the one "
                  "you set on AIOStreams' configure page, not the one you sign in to AIOStreams with.")


def _plain(e, doing, own_password=False, making=False):
    """An AIOStreams error in plain words, without the link or the password: the page's own step (`doing`) only, then
    AIOStreams' reason, its own words quoted as its words (./setup's words, e.message, stay its own).
    own_password: the call used the person's own password, so a refused config is about that password. making: the
    call made a config, which AIOStreams limits per hour. Told by the error's code and kind, never by words
    in its message, which may be AIOStreams' own."""
    if own_password and str(e.code) in WRONG_PASSWORD_CODES:
        return WRONG_PASSWORD
    if e.code == UNREACHABLE:
        return (f"{doing}: AIOSubtitles can't reach your AIOStreams from the internet. Check the link, and that "
                "your AIOStreams can be opened from outside your home network.")
    if e.code == TOO_SLOW:       # asked, so it is reached
        return f"{doing}: your AIOStreams took too long to answer. Try again in a few minutes."
    if e.code == UNREADABLE:     # it answered, so it is reached
        return f"{doing}: your AIOStreams answered in a way AIOSubtitles doesn't know. Try again in a few minutes."
    if is_limited(e):
        if making:
            return (f"{doing}: your AIOStreams limits how many configs one address can make in an hour, and "
                    "AIOSubtitles has made many there lately. Try again in an hour.")
        return f"{doing}: your AIOStreams is limiting requests from AIOSubtitles right now. Try again in a few minutes."
    if needs_sign_in(e):         # ./setup's advice to sign in is for whoever runs that AIOStreams
        return (f"{doing}: your AIOStreams doesn't let AIOSubtitles make or save configs there. Ask whoever runs your "
                "AIOStreams.")
    if _refused(e):
        return f'{doing}: your AIOStreams refused the config: "{e.detail}".'
    if e.detail:
        return f'{doing}: your AIOStreams answered "{e.detail}".'
    if e.answer:                 # an error answer without words: by its code, or its HTTP status
        return f"{doing}: your AIOStreams answered with an error ({e.answer}). Try again in a few minutes."
    return f"{doing}: {e.reason}"


FULL = "AIOSubtitles is full for now. Try again in a few days."


class SignupGate:
    """Checked before a sign-up contacts anything: the invite code, attempts per address per hour, the user cap (counted
    again where the record is written, Users.create, since sign-ups run at once pass this count together)."""

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
        # capitals don't matter: phones may type the code in capitals, setup suggests it in small letters
        if self.s.invite and not hmac.compare_digest(str(invite or "").strip().casefold().encode(),
                                                     self.s.invite.casefold().encode()):
            raise SignupError("That invite code isn't right. AIOSubtitles is open only to invited people for now.", 403,
                              field="invite")
        if self.count_users() >= self.s.max_users:
            raise SignupError(FULL, 503)


# ------------------------------------------------------------------------------------------- the steps

def _read_viewer(api, uuid, password, metadata=True):
    """(the person's config, the presets' details AIOStreams' status gives, for adding AIOSubtitles to it); without
    `metadata` the status isn't asked and the details are {}: the daily rebuild changes nothing in their config
."""
    try:
        status = api.status() if metadata else {}
        viewer, _ = api.get_config(uuid, password)
    except AiosError as e:
        if e.code == BAD_ANSWER:
            raise SignupError(f"{UNKNOWN_ANSWER}. Is the link from AIOStreams?") from None
        raise SignupError(_plain(e, "Reading your AIOStreams config", own_password=True), field=_field_of(e)) from None
    presets = (status.get("settings") or {}).get("presets") if isinstance(status.get("settings"), dict) else None
    metadata = {p["ID"]: p for p in presets or [] if isinstance(p, dict) and "ID" in p}
    return viewer, metadata


def _field_of(e):
    """The field an AIOStreams error is about: the password it refused, or the link it couldn't reach (or where
    something else than AIOStreams answered)."""
    if str(e.code) in WRONG_PASSWORD_CODES:
        return "password"
    if e.code in (UNREACHABLE, NOT_AIOSTREAMS):
        return "link"
    return None


# synced_at (when AIOSubtitles last copied the person's config, into either of its configs) is neither's: taking the
# sync config out leaves it
SYNC_FIELDS = ("sync_uuid", "sync_link", "sync_password", "sync_fp")
USENET_FIELDS = ("usenet_uuid", "usenet_link", "usenet_password", "usenet_fp", "usenet_subtitles")


def config_fields(record, kind):
    """(id, link, password) of one of AIOSubtitles' configs in a record: `kind` "sync" (the sync config) or "usenet"
    (the Usenet config)."""
    return tuple(record.get(f"{kind}_{name}") for name in ("uuid", "link", "password"))


def _take_config(record, kind):
    """One of AIOSubtitles' configs (`kind` as config_fields) taken out of the record: its (id, link, password) as they
    were, or None when it had none. The caller removes that config once the record is saved."""
    fields = config_fields(record, kind)
    for name in SYNC_FIELDS if kind == "sync" else USENET_FIELDS:
        record.pop(name, None)
    return fields if fields[0] else None


def store_only(record):
    """A record of a setup whose only debrid service is Real-Debrid: no sync config, nothing read."""
    return record.get("store_only") is True


def _to_store_only(record):
    """The record now for stored copies only: its sync config taken out (_take_config)."""
    fields = _take_config(record, "sync")
    record["store_only"] = True
    return fields


def wants_usenet(settings, form):
    """The person's Usenet box, ticked on an instance that lets people turn Usenet on."""
    return settings.usenet_offered and form.get("usenet") == "on"


def _usenet_for(viewer, manifest_url, subtitles):
    """(Usenet config, the services it reads through, None), or (None, [], why) when the viewer's config has no
    Usenet route (aiosconfig.usenet_route). `subtitles`: it also carries the subtitle sources (no sync config)."""
    services, why = usenet_route(viewer, manifest_url, names=OWN_NAMES)
    if why:
        return None, [], why
    return usenet_sync_config(viewer, manifest_url, names=OWN_NAMES, subtitles=subtitles), services, None


@dataclass
class ConfigPlan:
    """Which of AIOSubtitles' configs a person's config gets (_configs_for), made by sign-up, Refresh and the daily
    rebuild as it says: `sync` the sync config (None: none), `usenet` the Usenet config (None: none) with the Usenet
    `services` it reads through, or `why` it has none; `stored`: stored copies only; `warning`:
    explain.PUBLIC_REAL_DEBRID_WARNING when Real-Debrid is left out of what is made."""
    sync: dict = None
    usenet: dict = None
    services: list = field(default_factory=list)
    why: str = None
    stored: bool = False
    warning: tuple = None


def _configs_for(settings, viewer, manifest_url, want):
    """The ConfigPlan for a viewer's config, the one AIOSubtitles' configs are made from: a sync config when it has a
    debrid service AIOSubtitles may read; with `want` (the Usenet box) a Usenet config when it has a Usenet route;
    stored copies only when Real-Debrid is its only debrid service and there is no Usenet config. Raises
    SignupError (`unusable`) when there is no file AIOSubtitles could read."""
    real_debrid_only = realdebrid.real_debrid_only(viewer)
    sync = sync_config(viewer, manifest_url, names=OWN_NAMES)
    # `usenet`: this instance reads Usenet files too, so the reason a config can't be used names both
    problems = sync_config_problems(sync, usenet=settings.usenet)
    if real_debrid_only or problems:
        sync = None
    usenet, services, why = _usenet_for(viewer, manifest_url, sync is None) if want else (None, [], None)
    stored = real_debrid_only and usenet is None
    if sync is None and usenet is None and not stored:      # so not Real-Debrid-only: `problems` says why
        raise SignupError("AIOSubtitles can't find any file it could read through your AIOStreams: "
                          + "; ".join(problems) + ".", unusable=True)
    left_out = not stored and realdebrid.SERVICE in realdebrid.debrid_in_use(viewer)
    return ConfigPlan(sync=sync, usenet=usenet, services=services, why=why, stored=stored,
                      warning=explain.PUBLIC_REAL_DEBRID_WARNING if left_out else None)


def _usenet_state(record, want, services, why):
    """The person's Usenet box, and what came of it: the services read through, or why there is no Usenet config."""
    record["usenet"] = bool(want)
    for name, value in (("usenet_why", why if want else None), ("usenet_services", services if want else None)):
        if value:
            record[name] = value
        else:
            record.pop(name, None)


def _usenet_synced(record, usenet, fields, subtitles, now=None):
    """The Usenet config as AIOSubtitles made or saved it; `now`: when (sign-up and Refresh, as _synced for the sync
    config,; the daily rebuild's time is set by the check, recheck.py)."""
    record["usenet_uuid"], record["usenet_link"], record["usenet_password"] = fields
    record["usenet_fp"] = sync_fingerprint(usenet)
    record["usenet_subtitles"] = bool(subtitles)
    if now is not None:
        record["synced_at"] = now


def _log_fault(e, doing):
    """A best-effort step that failed, for the operator (the person is told by the page), never by its text, which may
    hold a link: AIOStreams' answer (an AiosError) by what it means (its kind) or else its code, when that is a code
    word or an HTTP status (a code of another shape stands for anything a person's AIOStreams chose to put there);
    anything else (a config of a shape AIOSubtitles doesn't know, or a fault of its own) by its kind of exception."""
    if not isinstance(e, AiosError):
        what = type(e).__name__
    elif e.kind:
        what = e.kind
    elif isinstance(e.code, int) or (isinstance(e.code, str) and CODE_WORD.fullmatch(e.code)):
        what = e.code
    else:
        what = "an error answer"
    log.warning("%s failed: %s", doing, what)


# AiosError codes of a call that got no answer saying how it went (none at all, none in time, or one AIOSubtitles can't
# read): a save may have gone through
NO_VERDICT = (UNREACHABLE, TOO_SLOW, UNREADABLE, BAD_ANSWER)


def _add_addon(api, uuid, password, viewer, manifest_url, metadata, clean_up=()):
    """(changes, added, manual, unchecked): AIOSubtitles placed in the person's own config and read back; with
    `clean_up` (extra.cleanable_presets) the subtitle addons it asks itself are taken out of that config. Never
    raises: it runs once the record is saved, and the page must still give the person their link. Not confirmed, they
    add it by hand: `manual` when nothing in their config changed (the change couldn't be made, or AIOStreams answered
    the save with an error); `unchecked` when the save was sent but couldn't be checked (no answer saying how it went,
    a failed read-back, or AIOSubtitles not in it), so it may have gone through and `changes` are what was sent
."""
    sent = False
    try:
        new, changes = viewer_change(viewer, manifest_url, metadata, name=ADDON_NAME, clean_up=clean_up)
        if not changes:
            return [], True, False, False
        try:
            api.save_config(uuid, password, new)
        except AiosError as e:
            sent = e.code in NO_VERDICT
            raise
        sent = True
        saved, _ = api.get_config(uuid, password)
        if any(is_subsync_preset(p, manifest_url, ()) for p in saved.get("presets") or []):
            return changes, True, False, False
        log.warning("adding AIOSubtitles to a config failed: saved, but not found when read back")
    except Exception as e:
        _log_fault(e, "adding AIOSubtitles to a config")
    return (changes, False, False, True) if sent else ([], False, True, False)


def sync_fingerprint(sync):
    """A short fingerprint of a sync config as AIOSubtitles last made it: the daily rebuild saves only when it differs."""
    return hashlib.sha256(json.dumps(sync, sort_keys=True).encode()).hexdigest()[:32]


@contextlib.contextmanager
def _removed_on_failure(api, made):
    """Configs this service made in the block (`made`: their (id, link, password), added as they are made) are removed
    again when the block fails, whatever the failure, so no config holds a person's keys with nothing using it."""
    try:
        yield made
    except Exception:
        for fields in made:
            drop_sync(api, fields)
        raise


def _new_sync(api, instance, sync):
    """A new sync config on `instance`, checked by its manifest: (id, link, password). Nothing is left behind when the
    check fails."""
    sync_password = new_key(24)
    try:
        sync_uuid, sync_encrypted = api.create_config(sync, sync_password)
    except AiosError as e:
        if e.code == BAD_ANSWER:
            raise SignupError(f"{UNKNOWN_ANSWER} when making its config.") from None
        raise SignupError(_plain(e, "Making AIOSubtitles' config on your AIOStreams", making=True),
                          refused=_refused(e)) from None
    with _removed_on_failure(api, [(sync_uuid, None, sync_password)]):
        try:
            api.addon_get(f"/stremio/{sync_uuid}/{sync_encrypted}", "/manifest.json")
        except AiosError as e:
            raise SignupError(f"{UNKNOWN_ANSWER} when checking its config." if e.code == BAD_ANSWER
                              else _plain(e, "Checking AIOSubtitles' config on your AIOStreams")) from None
    return sync_uuid, install_link(instance, sync_uuid, sync_encrypted), sync_password


def _put_sync(api, record, sync, gone=False, kind="sync"):
    """`sync` saved into the record's sync config (`kind` "usenet": its Usenet config), or into a new one when that one
    is gone (`gone`: known already, so not tried): (id, link, password)."""
    if not gone:
        try:
            api.save_config(record[f"{kind}_uuid"], record[f"{kind}_password"], sync)
            return record[f"{kind}_uuid"], record[f"{kind}_link"], record[f"{kind}_password"]
        except AiosError as e:
            if str(e.code) not in WRONG_PASSWORD_CODES:
                raise SignupError(_plain(e, "Updating AIOSubtitles' config on your AIOStreams"),
                                  refused=_refused(e)) from None
    return _new_sync(api, record["instance"], sync)


def drop_sync(api, fields):
    """Removes a config this service made and won't use, (id, link, password); best effort: True when it is gone,
    removed now or already (its AIOStreams no longer opens it, as the daily check reads that). Never raises,
    so a clean-up never hides the error that started it, nor stops before the next config."""
    try:
        api.delete_config(fields[0], fields[2])
        return True
    except AiosError as e:
        return str(e.code) in WRONG_PASSWORD_CODES
    except Exception as e:
        _log_fault(e, "removing a config")
        return False


def all_clear(settings, record, now):
    """A record after a sign-up, a refresh or a rebuild: nothing to report, the next check a period from now."""
    record.update(state=None, unreachable=0, checked_at=now, next_check=now + settings.recheck_hours * HOUR)
    record.pop("state_detail", None)
    record.pop("errors", None)
    record.pop("main_gone_since", None)


def _synced(settings, record, sync, fields, now):
    record["sync_uuid"], record["sync_link"], record["sync_password"] = fields
    record.update(sync_fp=sync_fingerprint(sync), synced_at=now)
    all_clear(settings, record, now)


@dataclass
class Rebuilt:
    """What a daily rebuild did (rebuild): whether any of the record's fields changed; whether the person's config
    became Real-Debrid-only, so the record is now for stored copies only; and the configs it no longer
    uses, (id, link, password) each, for the caller to remove once the record is saved. Kept apart from the record,
    which holds only its own fields, so saving it never keeps a removed config's password."""
    changed: bool = False
    to_store_only: bool = False
    drop: list = field(default_factory=list)


def _usable_now(settings, record):
    """Whether this record's Usenet box counts now: ticked, and this instance still lets people turn Usenet on."""
    return bool(record.get("usenet")) and settings.usenet_offered


def rebuild(settings, record, token, api, password, force=False, force_usenet=False):
    """With the password a person let AIOSubtitles keep: AIOSubtitles' configs made again from theirs, each saved only
    when it changed (or `force`, `force_usenet`: when it is gone). The record's fields are updated; returns a Rebuilt.
    Both ways: a stored-copies-only record whose config now has another debrid service (or, with Usenet on, a
    Usenet route) gets its configs; a config that became Real-Debrid-only without a Usenet route moves the record to
    stored copies only, its sync fields taken out. The Usenet config follows the person's box. Raises SignupError
    (field "password" when the password is refused). Their own config is never changed."""
    viewer, _ = _read_viewer(api, record["main_uuid"], password, metadata=False)
    manifest_url = personal_manifest_url(settings, token)
    plan = _configs_for(settings, viewer, manifest_url, _usable_now(settings, record))
    if plan.stored:
        usenet = _take_config(record, "usenet")
        _usenet_state(record, bool(record.get("usenet")), plan.services, plan.why)   # their box stays as they left it
        if store_only(record):
            return Rebuilt(changed=usenet is not None, drop=[usenet] if usenet else [])
        sync = _to_store_only(record)
        return Rebuilt(changed=True, to_store_only=True, drop=[c for c in (sync, usenet) if c])
    drop, changed, made = [], False, []
    # the Usenet config first: when it can't be made, nothing of the sync config's has changed yet
    if plan.usenet is None:
        gone = _take_config(record, "usenet")
        if gone:
            drop.append(gone)
            changed = True
    elif force_usenet or not record.get("usenet_uuid") or sync_fingerprint(plan.usenet) != record.get("usenet_fp"):
        fields = _put_sync(api, record, plan.usenet, gone=force_usenet or not record.get("usenet_uuid"), kind="usenet")
        if fields[0] != record.get("usenet_uuid"):
            made.append(fields)    # removed again if the sync config can't be made below
        _usenet_synced(record, plan.usenet, fields, plan.sync is None)
        changed = True
    before = (record.get("usenet_why"), record.get("usenet_services"))
    _usenet_state(record, bool(record.get("usenet")), plan.services, plan.why)
    changed = changed or before != (record.get("usenet_why"), record.get("usenet_services"))
    if plan.sync is None:                 # Usenet only: no sync config any more
        gone = _take_config(record, "sync")
        if gone:
            drop.append(gone)
            changed = True
        if store_only(record):
            record.pop("store_only", None)
            changed = True
        return Rebuilt(changed=changed, drop=drop)
    with _removed_on_failure(api, made):  # nothing left behind: the Usenet config made a moment ago goes too
        if store_only(record) or not record.get("sync_uuid"):
            fields = _new_sync(api, record["instance"], plan.sync)
            record.pop("store_only", None)
        elif not force and sync_fingerprint(plan.sync) == record.get("sync_fp"):
            return Rebuilt(changed=changed, drop=drop)
        else:
            fields = _put_sync(api, record, plan.sync, gone=force)
    record["sync_uuid"], record["sync_link"], record["sync_password"] = fields
    record["sync_fp"] = sync_fingerprint(plan.sync)
    return Rebuilt(changed=True, drop=drop)


def _audio_answer(choice, settings):
    """One of the answers this instance offers for files with no subtitles of their own: off, pick, and open
    while it allows opened syncs (SUBSYNC_USER_OPENED_PER_DAY above 0). SignupError otherwise."""
    choice = str(choice or "").strip().lower()
    allowed = AUDIO_CHOICES if settings.user_opened_per_day > 0 else tuple(c for c in AUDIO_CHOICES if c != "open")
    if choice not in allowed:
        raise SignupError("Choose one of the answers for files with no subtitles of their own.", field="audio")
    return choice


def keeps_password(settings, form):
    """Whether a sign-up or refresh keeps the person's password: they ticked the box, on an instance whose check, the
    only use of a kept password, is on (Settings.keeps_passwords; with it off, a password kept before goes at the next
    refresh)."""
    return form.get("keep_password") == "on" and settings.keeps_passwords


def form_audio(settings, form):
    """The sign-up's answer for files with no subtitles of their own: None when this instance doesn't ask it
    (its audio route is off) or the form leaves it out (the person then reads "pick", as everyone signed up before);
    refused when it is none of the answers offered."""
    if not settings.audio_route or form.get("audio") is None:
        return None
    return _audio_answer(form.get("audio"), settings)


def signup(settings, users, form, api_factory=public_api, timeout=PUBLIC_TIMEOUT):
    with _refusal_explained():
        return _signup(settings, users, form, api_factory, timeout)


def _signup(settings, users, form, api_factory, timeout):
    langs = _languages(form.get("languages"), settings.max_languages)
    audio = form_audio(settings, form)              # refused before anything is read or made
    instance, uuid, alias = parse_link(form.get("link"))
    password = _password(form)
    api = api_factory(instance, timeout)
    uuid = _config_id(api, uuid, alias)
    viewer, metadata = _read_viewer(api, uuid, password)
    token = new_token()
    manifest_url = personal_manifest_url(settings, token)
    # The clean-up goes with adding AIOSubtitles (alone it would leave fewer subtitles); AIOSubtitles' own config
    # then lacks those addons too, so it asks their sites itself: only the addons whose site it really asks
    clean_up = cleanable_presets(settings) if form.get("add_addon") == "on" and form.get("clean_up") == "on" else ()
    want = wants_usenet(settings, form)
    record = {"instance": instance, "main_uuid": uuid, "languages": langs, "version": 1}
    if audio:
        record["audio"] = audio
    base = without_builtin_twins(viewer, clean_up)[0] if clean_up else viewer
    # judged on what the configs are made from: the clean-up takes out only subtitle addons, none of them a debrid
    # service or a stream source, so the answer is the one for their own config. With Real-Debrid as the only debrid
    # service and no Usenet config, stored copies only: no sync config, nothing of theirs read; the clean-up is
    # left out, since AIOSubtitles then asks no subtitle site for them
    plan = _configs_for(settings, base, manifest_url, want)
    # nothing left behind: a config made here is removed again when a later step, or saving the record, fails
    with _removed_on_failure(api, []) as made:
        if plan.stored:
            clean_up = ()
            _to_store_only(record)
            all_clear(settings, record, users.clock())
        elif plan.sync is not None:
            fields = _new_sync(api, instance, plan.sync)
            made.append(fields)
            _synced(settings, record, plan.sync, fields, users.clock())
        else:                                                       # Usenet only
            all_clear(settings, record, users.clock())
        if plan.usenet is not None:
            fields = _new_sync(api, instance, plan.usenet)
            made.append(fields)
            _usenet_synced(record, plan.usenet, fields, plan.sync is None, users.clock())
        _usenet_state(record, want, plan.services, plan.why)
        # the person chose it (unticked by default): AIOSubtitles refreshes by itself
        if keeps_password(settings, form):
            record["auto_password"] = password
        # the record first: the person's own config gets the link only once the link works; the cap is counted again
        # with the write, and a sign-up past it removes the configs it made
        if not users.create(token, record, settings.max_users):
            raise SignupError(FULL, 503)
    changes, added, manual, unchecked = [], False, False, False
    if form.get("add_addon") == "on":
        changes, added, manual, unchecked = _add_addon(api, uuid, password, viewer, manifest_url, metadata, clean_up)
    log.info("signed up: %d language%s%s%s", len(langs), "" if len(langs) == 1 else "s",
             ", stored copies only" if plan.stored else ", Real-Debrid left out" if plan.warning else "",
             ", Usenet on" if plan.usenet is not None else "")
    return SignupResult(token=token, manifest_url=manifest_url, changes=changes, warning=plan.warning, added=added,
                        manual=manual, unchecked=unchecked, languages=langs, store_only=plan.stored,
                        usenet=plan.usenet is not None, usenet_why=plan.why if want else None,
                        sync_made=plan.sync is not None and not plan.stored)


def refresh(settings, users, token, form, api_factory=public_api, timeout=PUBLIC_TIMEOUT):
    """Rebuilds the sync config, and with the Usenet box ticked the Usenet config, from the person's config as it is now
    (and puts AIOSubtitles back if it is missing). A gone config is made again. With a new config link (`link`), that
    config becomes theirs; on another AIOStreams their configs move there and the old ones are removed. The kept
    password follows the box (`keep_password`): ticked keeps this password once the refresh works, unticked deletes a
    kept one at once, even when the refresh then fails. Unticking the Usenet box removes the Usenet config."""
    with _refusal_explained():
        return _refresh(settings, users, token, form, api_factory, timeout)


@contextlib.contextmanager
def _locked_record(users, token, missing=NOT_SIGNED_UP):
    """The person's record, opened under its lock (users.locked) and held for the block, so a refresh, the daily check,
    a setting and a delete never write over each other's changes; SignupError(`missing`) when there is none."""
    with users.locked(token):
        record = users.open(token)
        if record is None:
            raise SignupError(missing)
        yield record


def _refresh(settings, users, token, form, api_factory, timeout):
    with _locked_record(users, token) as record:
        # a kept password the box no longer keeps is deleted first, before anything is checked or read, so it goes
        # whatever becomes of the refresh
        if not keeps_password(settings, form) and record.pop("auto_password", None) is not None:
            users.update(token, record)
            log.info("a kept password was deleted")
        link = (form.get("link") or "").strip()
        instance, main_uuid, alias = parse_link(link) if link else (record["instance"], record["main_uuid"], None)
        moved = instance_key(instance) != instance_key(record["instance"])
        if not moved:              # the same AIOStreams, however the link spells it: kept as the record has it
            instance = record["instance"]
        password = _password(form)
        api = api_factory(instance, timeout)
        main_uuid = _config_id(api, main_uuid, alias)
        viewer, metadata = _read_viewer(api, main_uuid, password)
        manifest_url = personal_manifest_url(settings, token)
        want = wants_usenet(settings, form)
        was_stored = store_only(record)
        # stored copies, both ways
        plan = _configs_for(settings, viewer, manifest_url, want)
        old = []                  # configs they no longer use, removed once the record is saved
        fields = usenet_fields = None
        # configs made here: removed again when anything fails before the record holds them, so nothing holds their
        # keys unused
        with _removed_on_failure(api, []) as made:
            # the Usenet config first, as the daily rebuild: when it can't be made, nothing of the sync config's has
            # changed yet
            if plan.usenet is not None:
                if moved or not record.get("usenet_uuid"):
                    usenet_fields = _new_sync(api, instance, plan.usenet)
                else:
                    usenet_fields = _put_sync(api, record, plan.usenet, kind="usenet")
                if usenet_fields[0] != record.get("usenet_uuid"):
                    made.append(usenet_fields)
            if plan.sync is not None:
                if was_stored or moved or not record.get("sync_uuid"):
                    fields = _new_sync(api, instance, plan.sync)
                else:
                    fields = _put_sync(api, record, plan.sync)
                if fields[0] != record.get("sync_uuid"):
                    made.append(fields)
            if record.get("sync_uuid") and (plan.sync is None or moved):
                old.append((record["instance"], config_fields(record, "sync")))
            if record.get("usenet_uuid") and (plan.usenet is None or moved):
                old.append((record["instance"], config_fields(record, "usenet")))
            record["instance"], record["main_uuid"] = instance, main_uuid
            if plan.stored:
                _to_store_only(record)
                all_clear(settings, record, users.clock())
            elif plan.sync is not None:
                record.pop("store_only", None)
                _synced(settings, record, plan.sync, fields, users.clock())
            else:
                record.pop("store_only", None)
                _take_config(record, "sync")
                all_clear(settings, record, users.clock())
            if plan.usenet is not None:
                _usenet_synced(record, plan.usenet, usenet_fields, plan.sync is None, users.clock())
            else:
                _take_config(record, "usenet")
            _usenet_state(record, want, plan.services, plan.why)
            if keeps_password(settings, form):
                record["auto_password"] = password
            if not users.update(token, record):   # deleted meanwhile (it expired): nothing holds what was made here
                raise SignupError(NOT_SIGNED_UP)
        # the record first, as at sign-up: the person's own config is changed only once the record holds the refresh;
        # still under the record's lock, so a delete can't come in between
        changes, added, manual, unchecked = [], False, False, False
        if form.get("add_addon") == "on":
            changes, added, manual, unchecked = _add_addon(api, main_uuid, password, viewer, manifest_url, metadata)
    for where, gone in old:   # the configs they no longer use: removed
        drop_sync(api_factory(where, timeout), gone)
    log.info("refreshed a sync config%s%s%s", ", stored copies only" if plan.stored else
             ", Real-Debrid left out" if plan.warning else "",
             ", on another AIOStreams" if moved and not plan.stored else "",
             ", Usenet on" if plan.usenet is not None else "")
    return SignupResult(token=token, manifest_url=manifest_url, changes=changes, warning=plan.warning, added=added,
                        manual=manual, unchecked=unchecked, languages=record["languages"], store_only=plan.stored,
                        to_store_only=plan.stored and not was_stored, usenet=plan.usenet is not None,
                        usenet_why=plan.why if want else None, sync_made=plan.sync is not None and not plan.stored)


def forget_password(users, token):
    """Deletes a kept AIOStreams password at once (the settings page's button); nothing else changes."""
    with _locked_record(users, token) as record:
        if record.pop("auto_password", None) is not None:
            users.update(token, record)
            log.info("a kept password was deleted")


TMDB_KEY_SHAPE = re.compile(r"[A-Za-z0-9._-]{16,1024}")    # a v3 key (32 hex) or a Read Access Token (a JWT)


def set_tmdb_key(users, token, key, settings, check=None):
    """Saves a person's own TMDB key, when the instance asks each person for one: checked with TMDB once first (one
    request with it), kept in their encrypted record, used for their original-language lookups. Never logged."""
    from .origlang import check_key
    key = (key or "").strip()
    texts = explain.PUBLIC_TEXTS
    if not TMDB_KEY_SHAPE.fullmatch(key):
        raise SignupError(texts["tmdb_refused"], field="tmdb_key")
    ok = (check or check_key)(settings, key)
    if ok is None:
        raise SignupError(texts["tmdb_unreachable"], field="tmdb_key")
    if not ok:
        raise SignupError(texts["tmdb_refused"], field="tmdb_key")
    with _locked_record(users, token) as record:
        record["tmdb_key"] = key
        users.update(token, record)
    log.info("a person's TMDB key was saved")


def remove_tmdb_key(users, token):
    """Deletes a person's own TMDB key at once; nothing else changes."""
    with _locked_record(users, token) as record:
        if record.pop("tmdb_key", None) is not None:
            users.update(token, record)
            log.info("a person's TMDB key was deleted")


def set_languages(users, token, text, limit=MAX_LANGUAGES):
    """Saves a person's languages, at most `limit` (the instance's SUBSYNC_MAX_LANGUAGES). A record holding more from
    before a lowered limit keeps them until this save."""
    with _locked_record(users, token) as record:
        record["languages"] = _languages(text, limit)
        users.update(token, record)
    return record["languages"]


def set_audio(users, token, choice, settings):
    """Saves a person's answer for files with no subtitles of their own: off, pick or open; open only while
    the instance allows opened syncs (SUBSYNC_USER_OPENED_PER_DAY above 0), and none on an instance without the audio
    route. No call to AIOStreams."""
    if not settings.audio_route:
        raise SignupError("This service doesn't sync files from their sound.")
    choice = _audio_answer(choice, settings)
    with _locked_record(users, token) as record:
        record["audio"] = choice
        users.update(token, record)
    return choice


def delete(settings, users, token, form, api_factory=public_api, timeout=PUBLIC_TIMEOUT, on_confirmed=None):
    """Removes the sync config and the Usenet config from the person's AIOStreams, with their password also AIOSubtitles
    from their own config, then their record and usage. Under the record's lock, so a refresh or the daily check can't
    make a config for a record being deleted; once the password is accepted, the record is always deleted.
    `on_confirmed`: called under the lock once the delete is certain (a typed password accepted), before anything is
    removed: a mistyped password, which deletes nothing, leaves the person's service as it was. Returns what
    was removed, each config by itself so the page names the one left: "sync_removed" and "usenet_removed"
    (also when there was none), "addon_removed"."""
    with _locked_record(users, token, "This link isn't signed up (any more): there is nothing to delete.") as record:
        api = api_factory(record["instance"], timeout)
        outcome = {"sync_removed": False, "usenet_removed": False, "addon_removed": False}
        viewer = password = None
        if form.get("password"):   # checked before anything is deleted, so a mistyped password can be typed again
            password = _password(form)
            try:
                viewer, _ = api.get_config(record["main_uuid"], password)
            except AiosError as e:
                if e.code == BAD_ANSWER:
                    raise SignupError(f"{UNKNOWN_ANSWER}. Nothing was deleted.") from None
                raise SignupError(_plain(e, "Reading your AIOStreams config", own_password=True)
                                  + " Nothing was deleted.", field=_field_of(e)) from None
        if on_confirmed is not None:
            on_confirmed()
        for kind in ("sync", "usenet"):          # no sync config for stored copies only or Usenet only
            outcome[f"{kind}_removed"] = not record.get(f"{kind}_uuid") or drop_sync(api, config_fields(record, kind))
        if viewer is not None:
            manifest_url = personal_manifest_url(settings, token)
            try:
                new, taken_out = without_subsync(viewer, manifest_url, names=(ADDON_NAME.lower(),))
                if taken_out:
                    api.save_config(record["main_uuid"], password, new)
                outcome["addon_removed"] = True
            except Exception as e:
                _log_fault(e, "removing AIOSubtitles from a config")
        users.delete(token)
    log.info("deleted a record (sync config removed: %s, Usenet config removed: %s, addon removed: %s)",
             outcome["sync_removed"], outcome["usenet_removed"], outcome["addon_removed"])
    return outcome
