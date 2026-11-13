"""The public mode's web pages: the start page with the sign-up, the signed-up page, a person's settings, delete,
privacy, not found, error.

Built from the approved design out of
the parts in ui.py, with one stylesheet and one small script of our own (static/app.css, static/app.js, served by this
service) and the two self-hosted typefaces. Every page works without the script: the sign-up then opens by its
link and shows its questions one under another. Nothing is loaded from elsewhere, nothing is inline, and
every value from a person or a config is escaped. The texts come from explain.py, where each has its one explanation,
and UI_WORDS below; nothing else is shown.
"""
from . import explain, ui
from .aiosconfig import LANGUAGES
from .ui import e

_NAMES = {three: name for name, _, three in LANGUAGES}
_CHOICES = [(name, three) for name, _, three in LANGUAGES]

# The fixed words of the pages, each approved by the owner. A test checks that the pages show nothing else.
UI_WORDS = (
    "AIOSubtitles", "Privacy", "Questions", "Sign up", "Before you start", "Continue", "Back", "Invite", "AIOStreams",
    "Languages", "Config", "Invite code", "Your AIOStreams link", "Your AIOStreams password", "Show", "Hide",
    "Keep my password, so AIOSubtitles follows my config by itself", "Your AIOStreams config",
    "Add AIOSubtitles to my AIOStreams config", "Remove the subtitle addons AIOSubtitles now searches itself",
    "Add language", "First", "Second", "Third", "Fourth", "None", "Remove: ", "Sign-up didn't go through",
    "What AIOSubtitles reads and keeps: ", "Your personal addon link", "Copy link", "Copied", "You're signed up",
    "Done on your AIOStreams", "Changed in your AIOStreams config: ", "AIOSubtitles is in your AIOStreams config already.",
    "One step left", "Add AIOSubtitles to your AIOStreams config yourself:", "What happens next",
    "Open your settings page", "Your AIOSubtitles", "Today", "new files", "read", "Today's limit is reached",
    "Save languages", "Refresh after changing your AIOStreams config", "A new AIOStreams link (optional)", "Refresh",
    "Stop keeping my password", "Delete my data", "Delete my data…", "That didn't go through", "Delete your data?",
    "Your AIOStreams password (optional)", "Keep my data", "Nothing was deleted", "Your data is deleted",
    "Sign up again", "to use AIOSubtitles again.", "What AIOSubtitles reads and keeps", "This page doesn't exist",
    "to start over.", "Something went wrong", "Back to the start page", "https://…/stremio/…/manifest.json",
    "Signed up · AIOSubtitles", "Settings · AIOSubtitles", "Delete · AIOSubtitles", "Deleted · AIOSubtitles",
    "Privacy · AIOSubtitles", "Not found · AIOSubtitles",
)
LINK_PLACEHOLDER = "https://…/stremio/…/manifest.json"


def language_names(codes):
    return ", ".join(_NAMES.get(c, c) for c in codes)


def _problem(error, field, title, fields=(), extra=""):
    """The summary of a problem, with a link to the field it is about when the page has it; focused by the script."""
    if not error:
        return ""
    message = (f"<a href=\"#{e(field)}\">{e(error)}</a>" if field in fields else e(error))
    return ui.note("problem", None, title, role="alert", id="problem", focus=True,
                   extra=f"<p id=\"problem-message\">{message}</p>{extra}")


# ------------------------------------------------------------------------------------------- the start page and sign-up

STEPS = ("Invite", "AIOStreams", "Languages", "Config")
STEP_OF = {"invite": "Invite", "link": "AIOStreams", "password": "AIOStreams", "languages": "Languages", "config": "Config"}


def _headline():
    """The approved heading with its deliberate breaks: desktop "Subtitles, in sync / with the file you play.", phone
    "Subtitles, / in sync with / the file you play."
    """
    first, middle, last = explain.PUBLIC_HEADLINE
    word, _, rest = last.partition(" ")
    return (f"{e(first)}<br class=\"phone\"> {e(middle)} <br class=\"wide\">{e(word)}<br class=\"phone\"> {e(rest)}")


def _intro():
    """The intro in two paragraphs, split at a sentence end (no word changed)."""
    text = explain.PUBLIC_TEXTS["intro"]
    at = text.find(" A file synced once")
    parts = [text[:at], text[at + 1:]] if at > 0 else [text]
    return "".join(f"<p>{e(p)}</p>" for p in parts)


def _screen(name, steps, fill, content, last=False):
    """One screen of the sign-up: its question, field or choices, Continue (Sign up on the last). `fill`: how much of
    its step's segment is filled once it shows (50 or 100)."""
    step = steps.index(STEP_OF[name])
    action = (ui.button("Sign up", "primary", type="submit", arrow=True)
              + f"<p class=\"after\">{e(explain.PUBLIC_TEXTS['password_once'])} What AIOSubtitles reads and keeps: "
              "<a href=\"/privacy\">Privacy</a>.</p>") if last else ui.button("Continue", "primary", attrs="data-next",
                                                                              arrow=True)
    return (f"<section class=\"screen\" data-screen=\"{name}\" data-step=\"{step}\" data-fill=\"{fill}\">{content}"
            f"<div class=\"actions\">{action}</div></section>")


def signup_page(settings, error=None, field=None, order=None):
    """The start page and the sign-up layer. After a problem the layer is open with the summary first and the field
    marked; the languages keep their order; the link, the password and the invite code are never sent back."""
    order = ["eng"] if order is None else order
    limit = settings.max_languages or 4
    steps = STEPS if settings.invite else STEPS[1:]
    # the summary sits right above every screen: a field with the problem is marked and described by it
    err = lambda name: "problem-message" if error and field == name else None  # noqa: E731
    screens = []
    if settings.invite:
        screens.append(_screen("invite", steps, 100, ui.field(
            "invite", "Invite code", explain.PUBLIC_FIELDS["invite"], mono=True, code=True, question=True,
            error_ref=err("invite"), attrs="required autocomplete=\"off\" autocapitalize=\"characters\" spellcheck=\"false\"")))
    screens.append(_screen("link", steps, 50, ui.field(
        "link", "Your AIOStreams link", explain.PUBLIC_FIELDS["link"], kind="url", mono=True, question=True,
        error_ref=err("link"), attrs=f"required autocomplete=\"off\" spellcheck=\"false\" inputmode=\"url\" "
                                 f"placeholder=\"{e(LINK_PLACEHOLDER)}\"")))
    screens.append(_screen("password", steps, 100, ui.field(
        "password", "Your AIOStreams password", explain.PUBLIC_FIELDS["password"], kind="password", mono=True,
        question=True, reveal=True, error_ref=err("password"), attrs="required autocomplete=\"off\"")
        + f"<div class=\"choices\">{ui.choice('keep_password', 'Keep my password, so AIOSubtitles follows my config by itself', explain.PUBLIC_FIELDS['keep_password'])}</div>"))
    screens.append(_screen("languages", steps, 100,
                           "<h2 class=\"q\" id=\"languages-q\">Languages</h2>"
                           + f"<p class=\"help\" id=\"languages-help\">{e(explain.languages_help(limit))}</p>"
                           + ui.language_slots(_CHOICES, order, limit, "languages-q", "languages-help",
                                               err("languages"))))
    screens.append(_screen("config", steps, 100, "<h2 class=\"q\">Your AIOStreams config</h2>" + ui.choices(
        ui.choice("add_addon", "Add AIOSubtitles to my AIOStreams config", explain.PUBLIC_FIELDS["add_addon"], checked=True),
        ui.choice("clean_up", "Remove the subtitle addons AIOSubtitles now searches itself",
                  explain.PUBLIC_FIELDS["clean_up"], needs="add_addon")), last=True))
    fields = ("link", "password", "languages") + (("invite",) if settings.invite else ())
    problem = _problem(error, field, "Sign-up didn't go through", fields,
                       extra=f"<p>{e(explain.PUBLIC_TEXTS['kept_note'])}</p>")
    # without the script the arrow is a plain link back to the start page
    arrow_back = (f"<a class=\"icon-btn\" href=\"/\" data-back aria-label=\"Back\">{ui.BACK}</a>")
    layer = (f"<div class=\"layer\" id=\"signup\"><form class=\"sheet\" method=\"post\" action=\"/signup\" role=\"dialog\" "
             f"aria-modal=\"true\" aria-label=\"Sign up\" novalidate data-signup>"
             f"<div class=\"sheet-top\">{arrow_back}<ol class=\"steps\" id=\"steps\">"
             + "".join(f"<li><i></i>{e(s)}</li>" for s in steps) + "</ol></div>"
             f"<div class=\"screens\">" + (f"<div class=\"notes\">{problem}</div>" if problem else "")
             + "".join(screens) + "</div></form></div>")
    before = ui.dots(explain.PUBLIC_BEFORE)
    tries = explain.PUBLIC_TEXTS["tries"].format(n=settings.signups_per_hour)
    opener = ui.link_button("Sign up", "#signup", "primary", attrs="data-open", arrow=True)
    body = (f"<div class=\"wrap\">{ui.top(explain.CONTACT)}<main class=\"grid\">"
            f"<h1>{_headline()}</h1><section class=\"what\">{_intro()}</section>"
            f"<section class=\"need\" aria-labelledby=\"need-h\"><h2 id=\"need-h\">Before you start</h2>{before}"
            f"<p class=\"small\">{e(tries)}</p><div class=\"go\">{opener}</div></section>"
            "<div class=\"page-end\" aria-hidden=\"true\"></div></main></div>"
            f"<div class=\"dock\">{opener}</div>{layer}")
    if not error:
        return ui.page("AIOSubtitles", body, "start")
    # the layer comes back open, at the screen with the problem; the script takes it from there
    at = field if field in fields else ("invite" if settings.invite else "link")
    return ui.page("AIOSubtitles", body, "start", body_attrs=f" data-open-at=\"{e(at)}\"", opened=True)


# ------------------------------------------------------------------------------------------- signed up

def _settings_link(manifest_url):
    return manifest_url[: -len("manifest.json")] + "configure"


def result_page(result):
    texts = explain.PUBLIC_TEXTS
    stored = getattr(result, "store_only", False)
    done = [] if stored else [texts["made_config"]]
    if result.added:
        done += [f"Changed in your AIOStreams config: {c}" for c in result.changes] or \
                ["AIOSubtitles is in your AIOStreams config already."]
    if result.languages:
        done.append(texts["languages_chosen"].format(names=language_names(result.languages)))
    notes = []
    if stored:
        notes.append(ui.note("warn", explain.PUBLIC_STORE_ONLY[1], explain.PUBLIC_STORE_ONLY[0], role="status"))
    if result.warning:
        head, sep, rest = result.warning.partition(": ")
        notes.append(ui.note("warn", rest[:1].upper() + rest[1:], head, role="status") if sep
                     else ui.note("warn", result.warning, role="status"))
    left = ""
    if not result.added:
        lines = ([f"<p>{e(texts['not_added'])}</p>"] if result.manual else []) + \
                ["<p>Add AIOSubtitles to your AIOStreams config yourself:</p>", ui.dots(explain.PUBLIC_MANUAL_STEPS)]
        left = ui.note("warn", None, "One step left", extra="".join(lines))
    # the personal link first; what to know about this setup heads the column beside it
    body = (f"<div class=\"wrap\">{ui.top(explain.CONTACT)}<main class=\"grid\"><h1>You're signed up</h1>"
            + f"<section class=\"key\" aria-labelledby=\"personal-l\">"
            + ui.link_block(result.manifest_url, texts["signed_up"])
            + "</section><div class=\"then\">"
            + (f"<div class=\"notes\">{''.join(notes)}</div>" if notes else "")
            + "<section aria-labelledby=\"done-h\"><h2 id=\"done-h\">Done on your AIOStreams"
            f"</h2>{ui.dots(done)}{left}</section><section aria-labelledby=\"next-h\"><h2 id=\"next-h\">What happens next</h2>"
            f"<p>{e(texts['next'])}</p>"
            + ui.link_button("Open your settings page", _settings_link(result.manifest_url), "secondary", arrow=True)
            + f"<p class=\"after\">{e(texts['settings_link'])}</p></section></div></main></div>")
    return ui.page("Signed up · AIOSubtitles", body, "done", personal=True)


# ------------------------------------------------------------------------------------------- settings

def _megabytes(nbytes):
    """A read amount: one decimal under 10 MB (at least 0.1 when anything was read), whole MB from 10 MB."""
    mb = nbytes / (1024 * 1024)
    if nbytes <= 0:
        return "0"
    return f"{max(mb, 0.1):.1f}" if mb < 10 else f"{mb:.0f}"


def reset_in(now):
    """How long until 00:00 UTC, in plain words: "in 7 h 12 min", "in 1 min", "in 24 h"."""
    left = 86400 - int(now) % 86400
    minutes = -(-left // 60)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"in {minutes} min"
    return f"in {hours} h" + (f" {minutes} min" if minutes else "")


def _share(used, limit):
    if used <= 0 or limit <= 0:
        return 0
    return max(0.015, min(1.0, used / limit))


DONE = ("languages", "refreshed", "refreshed-rd", "refreshed-manual", "forgot", "to-stored", "to-stored-manual", "stored",
        "stored-manual")


def done_message(code, record):
    """(message, notice) for the page shown after a save: fixed texts only, chosen by a known code."""
    texts = explain.PUBLIC_TEXTS
    if code == "languages":
        return texts["languages_saved"].format(names=language_names(record["languages"])), None
    if code == "refreshed":
        return texts["refreshed"], None
    if code == "refreshed-rd":
        return texts["refreshed"], explain.PUBLIC_REAL_DEBRID_WARNING
    if code == "refreshed-manual":
        return None, " ".join((texts["refresh_manual"],) + explain.PUBLIC_MANUAL_STEPS)
    if code == "forgot":
        return texts["forgot"], None
    # stored copies only: moved there by this refresh, or still there (the page's note says the rest)
    manual = " ".join((texts["not_added"],) + explain.PUBLIC_MANUAL_STEPS)
    if code in ("to-stored", "to-stored-manual"):
        return texts["to_stored"], manual if code.endswith("-manual") else None
    if code == "stored-manual":
        return None, manual
    return None, None


def days_ago(then, now):
    """When something happened, in plain words: "today", "yesterday", "5 days ago"; "at sign-up" when unknown."""
    if not then:
        return "at sign-up"
    days = int(now // 86400) - int(then // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def _state_note(record, state):
    """What the daily check found (or the live reads-refused state), as a note at the top of the settings page."""
    state = state or record.get("state")
    if state not in explain.PUBLIC_STATES:
        return ""
    title, text, _ = explain.PUBLIC_STATES[state]
    return ui.note("warn", text.replace("{detail}", record.get("state_detail") or ""), title, role="status")


def _form(action, content, cls="controls"):
    """A settings form: posts to the page itself with its action's name."""
    return (f"<form{f' class=\"{cls}\"' if cls else ''} method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" "
            f"value=\"{e(action)}\">{content}</form>")


def _row(hid, heading, about, controls_html):
    """A settings row: its heading and explanation on the left, its controls on the right (stacked on phones)."""
    return (f"<section class=\"grid12 row\" aria-labelledby=\"{hid}\"><div class=\"about\"><h2 id=\"{hid}\">{e(heading)}</h2>"
            f"{about}</div>{controls_html}</section>")


def settings_page(settings, record, usage, manifest_url, message=None, error=None, notice=None, now=0, field=None,
                  state=None):
    texts = explain.PUBLIC_TEXTS
    stored = record.get("store_only") is True
    limit = settings.max_languages or 4
    files_limit, bytes_limit = settings.user_files_per_day, settings.user_daily_bytes
    full = usage["files"] >= files_limit or usage["bytes"] >= bytes_limit
    notes = (ui.note("ok", message, role="status") if message else "") \
        + (ui.note("warn", notice, role="status") if notice else "") \
        + (ui.note("warn", explain.PUBLIC_STORE_ONLY[1], explain.PUBLIC_STORE_ONLY[0]) if stored else "") \
        + _state_note(record, state) \
        + (ui.note("warn", texts["limit_reached"], "Today's limit is reached") if full and not stored else "") \
        + _problem(error, field, "That didn't go through", ("refresh", "new_link", "languages"))
    today = "" if stored else (
        "<section class=\"today\" aria-labelledby=\"today-h\"><h2 id=\"today-h\">Today</h2>"
        + ui.gauge(f"{usage['files']} of {files_limit}", "new files", _share(usage["files"], files_limit),
                   usage["files"] >= files_limit)
        + ui.gauge(f"{_megabytes(usage['bytes'])} MB of {bytes_limit / (1024 * 1024):.0f} MB", "read",
                   _share(usage["bytes"], bytes_limit), usage["bytes"] >= bytes_limit)
        + f"<p class=\"help\">{e(texts['reset'].format(when=reset_in(now)))}</p></section>")
    link = (f"<section class=\"key\" aria-labelledby=\"personal-l\">"
            + ui.link_block(manifest_url, texts["settings_lead"])
            + "</section>")
    lang_error = error if field == "languages" else None
    languages = _row("languages-h", "Languages",
                     (f"<p class=\"field-error\" id=\"languages-error\">{e(lang_error)}</p>" if lang_error else "")
                     + f"<p class=\"help\" id=\"languages-help\">{e(explain.languages_help(limit))}</p>",
                     _form("languages", ui.language_slots(_CHOICES, record["languages"], limit, "languages-h",
                                                          "languages-help", "languages-error" if lang_error else None)
                           + f"<div class=\"actions\">{ui.button('Save languages', 'primary', type='submit')}</div>"))
    kept = bool(record.get("auto_password"))
    copied = "" if stored else (f"<p class=\"help\">"
                                f"{e(texts['copied_auto' if kept else 'copied'].format(when=days_ago(record.get('synced_at'), now)))}</p>")
    forget = _form("forget-password", "<div class=\"actions\">" + ui.button(
        "Stop keeping my password", "secondary", type="submit", attrs="aria-describedby=\"forget_password-help\"")
        + f"</div><p class=\"after\" id=\"forget_password-help\">{e(explain.PUBLIC_FIELDS['forget_password'])}</p>",
        cls="forget") if kept else ""
    refresh = _row("refresh-h", "Refresh after changing your AIOStreams config", copied, "<div class=\"controls\">"
                   + _form("refresh", ui.field("refresh", "Your AIOStreams password", explain.PUBLIC_FIELDS["refresh"], name="password",
                            kind="password", mono=True, reveal=True, error=error if field == "refresh" else None,
                            attrs="required autocomplete=\"off\"")
                   + ui.field("new_link", "A new AIOStreams link (optional)", explain.PUBLIC_FIELDS["new_link"],
                              name="link", kind="url", mono=True, error=error if field == "new_link" else None,
                              attrs=f"autocomplete=\"off\" spellcheck=\"false\" inputmode=\"url\" "
                                    f"placeholder=\"{e(LINK_PLACEHOLDER)}\"")
                   + ui.choices(ui.choice("keep_password", "Keep my password, so AIOSubtitles follows my config by itself",
                                          explain.PUBLIC_FIELDS["keep_password"], checked=kept))
                   + "<input type=\"hidden\" name=\"add_addon\" value=\"on\">"
                   + f"<div class=\"actions\">{ui.button('Refresh', 'primary', type='submit')}</div>", cls="")
                   + forget + "</div>")
    delete = _row("delete-h", "Delete my data",
                  f"<p class=\"help\" id=\"delete-help\">{e(explain.PUBLIC_FIELDS['delete'])}</p>",
                  _form("delete-confirm", "<div class=\"actions\">" + ui.button(
                      "Delete my data…", "danger", type="submit", attrs="aria-describedby=\"delete-help\"") + "</div>"))
    body = (f"<div class=\"wrap\">{ui.top(explain.CONTACT)}<main><h1>Your AIOSubtitles</h1>"
            + (f"<div class=\"notes\" aria-live=\"polite\">{notes}</div>" if notes else "")
            + f"<div class=\"grid12 band\">{link}{today}</div>"
            f"<div class=\"rows\">{languages}{refresh}{delete}</div></main></div>")
    return ui.page("Settings · AIOSubtitles", body, "settings", personal=True)


# ------------------------------------------------------------------------------------------- delete, privacy, errors

def _letter(title, heading, content, personal=False, start="/"):
    body = (f"<div class=\"wrap\">{ui.top(explain.CONTACT, start)}<main class=\"letter\"><h1>{e(heading)}</h1>{content}</main>"
            "</div>")
    return ui.page(title, body, "letter", personal=personal)


def delete_confirm_page(error=None, field=None, stored=False):
    """The delete step. `stored`: a person with stored copies only has no config of AIOSubtitles' own to remove."""
    gone = [line for i, line in enumerate(explain.PUBLIC_DELETE_LIST) if not (stored and i == 1)]
    content = (_problem(error, field, "Nothing was deleted", ("delete",))
               + f"<div class=\"body\"><div><p class=\"lead\">{e(explain.PUBLIC_TEXTS['undone'])}</p>{ui.dots(gone)}</div></div>"
               + _form("delete", ui.field("delete", "Your AIOStreams password (optional)", explain.PUBLIC_FIELDS["delete"], name="password",
                          kind="password", mono=True, reveal=True,
                          error_ref="problem-message" if error and field == "delete" else None,
                          attrs="autocomplete=\"off\"")
               + "<div class=\"actions\">" + ui.button("Delete my data", "danger", type="submit")
               + ui.link_button("Keep my data", "configure", "secondary") + "</div>", cls=""))
    return _letter("Delete · AIOSubtitles", "Delete your data?", content, personal=True)


def deleted_page(outcome):
    left = []
    if not outcome.get("sync_removed"):
        left.append(explain.PUBLIC_TEXTS["sync_left"])
    if not outcome.get("addon_removed"):
        left.append(explain.PUBLIC_TEXTS["addon_left"])
    notes = ui.note("ok", explain.PUBLIC_TEXTS["deleted"], role="status")
    if left:
        notes += ui.note("warn", None, "One step left", extra="".join(f"<p>{e(t)}</p>" for t in left))
    content = (f"<div class=\"notes\">{notes}</div><div class=\"body\"><p><a href=\"/\">Sign up again</a> "
               "to use AIOSubtitles again.</p></div>")
    return _letter("Deleted · AIOSubtitles", "Your data is deleted", content, personal=True)


def privacy_page(settings):
    sections = "".join(
        f"<section aria-labelledby=\"privacy-{i}\"><h2 id=\"privacy-{i}\">{e(head)}</h2>"
        f"<p>{e(text[:1].upper() + text[1:])}</p></section>" for i, (head, _, text) in enumerate(explain.PRIVACY_PARTS))
    content = (f"<div class=\"body\">{sections}<section aria-labelledby=\"privacy-q\"><h2 id=\"privacy-q\">Questions</h2>"
               f"<p><a class=\"mono\" href=\"{e(explain.CONTACT)}\">{e(explain.CONTACT)}</a></p></section></div>")
    return _letter("Privacy · AIOSubtitles", "What AIOSubtitles reads and keeps", content)


def not_found_page():
    content = (f"<div class=\"body\"><p>{e(explain.PUBLIC_TEXTS['not_found'])}</p>"
               "<p><a href=\"/\">Sign up</a> to start over.</p></div>")
    return _letter("Not found · AIOSubtitles", "This page doesn't exist", content)


def error_page(message):
    content = (f"<div class=\"notes\">{ui.note('problem', message, role='alert')}</div>"
               "<div class=\"body\"><p><a href=\"/\">Back to the start page</a></p></div>")
    return _letter("AIOSubtitles", "Something went wrong", content)
