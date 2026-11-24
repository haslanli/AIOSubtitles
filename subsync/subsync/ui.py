"""The parts AIOSubtitles' pages are built from, and the parts the installer reuses.

Each part is a small function that returns HTML. Every value is escaped here; what a part takes as HTML (`attrs`,
`body_attrs`, `extra`, the `*_html` parameters) its caller built from escaped parts. No part writes a style attribute
(the security header allows only static/app.css). The look is in static/app.css, the few moments that move in
static/app.js, and the words come from the caller (explain.py and pages.py hold the approved texts).
"""
import functools
import hashlib
import html
import os

from .langs import DEFAULT_LANGUAGE
from .version import PROJECT

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FONT_FILES = ("AtkinsonHyperlegibleNext-wght.ttf", "AtkinsonHyperlegibleMono-wght.ttf")   # put in by the image build


def e(value):
    return html.escape(str(value), quote=True)


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


APP_CSS = _read("app.css")
APP_JS = _read("app.js")
# the asset addresses carry a short hash of their content, so a cache (Cloudflare's included) never pairs a new page
# with an old stylesheet or script
CSS_URL = "/static/app.css?v=" + hashlib.sha256(APP_CSS.encode()).hexdigest()[:10]
JS_URL = "/static/app.js?v=" + hashlib.sha256(APP_JS.encode()).hexdigest()[:10]

# the HTTP headers of the public site's pages and files, and of the installer's setup page (which adds one source)
PAGE_HEADERS = {
    # only this service's own script, stylesheet and two typefaces, nothing inline
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    # no-transform: a proxy in front passes every page through unchanged (Cloudflare added its analytics script to
    # one)
    "Cache-Control": "no-store, no-transform"}
SCRIPT_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=3600",
                  "Referrer-Policy": "no-referrer"}
# the typefaces never change under their names (a new version would get a new name)
FONT_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=31536000, immutable",
                "Referrer-Policy": "no-referrer"}


def font_path(name):
    """The file of one of the two typefaces, or None when the name is not one of them or the file is missing
    (a source checkout: the image build puts them in; pages then fall back to the system font)."""
    if name not in FONT_FILES:
        return None
    path = os.path.join(STATIC, "fonts", name)
    return path if os.path.isfile(path) else None


@functools.lru_cache(maxsize=len(FONT_FILES) + 1)
def font_bytes(path):
    """A typeface file's bytes, read once (they never change while the service runs)."""
    with open(path, "rb") as f:
        return f.read()


# ------------------------------------------------------------------------------------------- icons (drawn, no images)

def _svg(paths, width="2.25", join=True):
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + width + '" stroke-linecap="round"'
            + (' stroke-linejoin="round"' if join else '') + ' aria-hidden="true" focusable="false">' + paths + '</svg>')


ARROW = _svg('<path d="M5 12h14M13 6l6 6-6 6"/>')
BACK = _svg('<path d="M15 5l-7 7 7 7"/>', "2")
PLUS = _svg('<path d="M12 5v14M5 12h14"/>', "2.25", join=False)
CROSS = _svg('<path d="M6 6l12 12M18 6L6 18"/>', "2", join=False)
CHEVRON = _svg('<path d="M6 9l6 6 6-6"/>', "2")
COPY = _svg('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>', "2")
TICK = _svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>', "2.25")
TICK_BOLD = _svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>', "2.5")
CROSS_BOLD = _svg('<path d="M6 6l12 12M18 6L6 18"/>', "2.5", join=False)
MARK = _svg('<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>', "2")


# TMDB's logo, for its attribution (its terms ask for its logo and notice wherever its data is used): the "alt short"
# logo from themoviedb.org/about/logos-attribution (blue_short-8e7b30f7….svg, whose SHA-256 is the hash in its name),
# drawn here as it is, its gradient given as attributes in place of the file's style element (the pages allow none)
_TMDB_PATH = ("M191.85,35.37h63.9A17.67,17.67,0,0,0,273.42,17.7h0A17.67,17.67,0,0,0,255.75,0h-63.9A17.67,17.67,0,0,"
             "0,174.18,17.7h0A17.67,17.67,0,0,0,191.85,35.37ZM10.1,35.42h7.8V6.92H28V0H0v6.9H10.1Zm28.1,0H46V8.25h"
             ".1L55.05,35.4h6L70.3,8.25h.1V35.4h7.8V0H66.45l-8.2,23.1h-.1L50,0H38.2ZM89.14.12h11.7a33.56,33.56,0,0"
             ",1,8.08,1,18.52,18.52,0,0,1,6.67,3.08,15.09,15.09,0,0,1,4.53,5.52,18.5,18.5,0,0,1,1.67,8.25,16.91,16"
             ".91,0,0,1-1.62,7.58,16.3,16.3,0,0,1-4.38,5.5,19.24,19.24,0,0,1-6.35,3.37,24.53,24.53,0,0,1-7.55,1.15"
             "H89.14Zm7.8,28.2h4a21.66,21.66,0,0,0,5-.55A10.58,10.58,0,0,0,110,26a8.73,8.73,0,0,0,2.68-3.35,11.9,1"
             "1.9,0,0,0,1-5.08,9.87,9.87,0,0,0-1-4.52,9.17,9.17,0,0,0-2.63-3.18A11.61,11.61,0,0,0,106.22,8a17.06,1"
             "7.06,0,0,0-4.68-.63h-4.6ZM133.09.12h13.2a32.87,32.87,0,0,1,4.63.33,12.66,12.66,0,0,1,4.17,1.3,7.94,7"
             ".94,0,0,1,3,2.72,8.34,8.34,0,0,1,1.15,4.65,7.48,7.48,0,0,1-1.67,5,9.13,9.13,0,0,1-4.43,2.82V17a10.28"
             ",10.28,0,0,1,3.18,1,8.51,8.51,0,0,1,2.45,1.85,7.79,7.79,0,0,1,1.57,2.62,9.16,9.16,0,0,1,.55,3.2,8.52"
             ",8.52,0,0,1-1.2,4.68,9.32,9.32,0,0,1-3.1,3A13.38,13.38,0,0,1,152.32,35a22.5,22.5,0,0,1-4.73.5h-14.5Z"
             "m7.8,14.15h5.65a7.65,7.65,0,0,0,1.78-.2,4.78,4.78,0,0,0,1.57-.65,3.43,3.43,0,0,0,1.13-1.2,3.63,3.63,"
             "0,0,0,.42-1.8A3.3,3.3,0,0,0,151,8.6a3.42,3.42,0,0,0-1.23-1.13A6.07,6.07,0,0,0,148,6.9a9.9,9.9,0,0,0-"
             "1.85-.18h-5.3Zm0,14.65h7a8.27,8.27,0,0,0,1.83-.2,4.67,4.67,0,0,0,1.67-.7,3.93,3.93,0,0,0,1.23-1.3,3."
             "8,3.8,0,0,0,.47-1.95,3.16,3.16,0,0,0-.62-2,4,4,0,0,0-1.58-1.18,8.23,8.23,0,0,0-2-.55,15.12,15.12,0,0"
             ",0-2.05-.15h-5.9Z")
TMDB_LOGO = ('<svg class="tmdb-logo" viewBox="0 0 273.42 35.52" role="img" aria-label="TMDB" focusable="false">'
             '<defs><linearGradient id="tmdb-g" y1="17.76" x2="273.42" y2="17.76" gradientUnits="userSpaceOnUse">'
             '<stop offset="0" stop-color="#90cea1"/><stop offset="0.56" stop-color="#3cbec9"/>'
             '<stop offset="1" stop-color="#00b3e5"/></linearGradient></defs>'
             '<path fill="url(#tmdb-g)" d="' + _TMDB_PATH + '"/></svg>')


def tmdb_credit(notice):
    """TMDB's logo with its notice, smaller than the service's own name."""
    return f'<p class="tmdb-credit">{TMDB_LOGO}<span>{e(notice)}</span></p>'


def footer(notice):
    """The foot of a page: TMDB's credit, where the service uses TMDB."""
    return f'<footer class="foot">{tmdb_credit(notice)}</footer>'


# ------------------------------------------------------------------------------------------- the page frame

def page(title, body, kind, personal=False, body_attrs="", opened=False, refresh=None):
    """A whole page around `body` (which starts with top()). `kind` names the layout: start, done, settings, letter.
    Personal pages ask not to be indexed. `body_attrs`: more attributes of <body> (escaped by the caller); `opened`:
    the sign-up layer comes back open (after a problem); `refresh`: seconds after which the page shows its work's
    progress again (the installer's): without the script it loads itself again; with it, the script fetches the page
    and puts in only what changed, so a screen reader isn't sent back to the top (data-refresh)."""
    if refresh:
        body_attrs = f" data-refresh=\"{int(refresh)}\"{body_attrs}"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            + ("<meta name=\"robots\" content=\"noindex\">" if personal else "")
            + (f"<noscript><meta http-equiv=\"refresh\" content=\"{int(refresh)}\"></noscript>" if refresh else "")
            + f"<title>{e(title)}</title><link rel=\"preload\" href=\"/static/fonts/{FONT_FILES[0]}\" as=\"font\" "
            f"type=\"font/ttf\" crossorigin><link rel=\"stylesheet\" href=\"{CSS_URL}\">"
            f"<script src=\"{JS_URL}\" defer></script></head><body class=\"p-{e(kind)}{' open' if opened else ''}\"{body_attrs}>"
            f"{body}</body></html>")


def top(contact, start="/", site=True):
    """The page's header: the name, Privacy and Source (the site's pages only, `site`; Source links the project's
    repository, the source the AGPL asks a service people use over the network to offer) and Questions (where
    there is a `contact`)."""
    return (f"<header class=\"top\"><a class=\"wordmark\" href=\"{e(start)}\">AIOSubtitles</a>"
            f"<nav class=\"links\" aria-label=\"AIOSubtitles\">"
            + (f"<a href=\"/privacy\">Privacy</a><a href=\"{e(PROJECT)}\">Source</a>" if site else "")
            + (f"<a href=\"{e(contact)}\">Questions</a>" if contact else "") + "</nav></header>")


# ------------------------------------------------------------------------------------------- buttons

def button(label, kind="primary", type="button", attrs="", arrow=False):
    return (f"<button class=\"btn {e(kind)}\" type=\"{e(type)}\"{(' ' + attrs) if attrs else ''}>{e(label)}"
            f"{ARROW if arrow else ''}</button>")


def link_button(label, href, kind="primary", attrs="", arrow=False):
    return (f"<a class=\"btn {e(kind)}\" href=\"{e(href)}\"{(' ' + attrs) if attrs else ''}>{e(label)}"
            f"{ARROW if arrow else ''}</a>")


# ------------------------------------------------------------------------------------------- fields

def described(*ids):
    return " ".join(i for i in ids if i)


# a Show/Hide button is named this and its box's label, so a list of the page's controls tells them apart; it
# shows only Show or Hide, and has no pressed state, as its label changes
SHOW, HIDE = "Show: ", "Hide: "


def field(id, label, help_text, name=None, kind="text", mono=False, error=None, attrs="", reveal=False, question=False,
          code=False, error_ref=None):
    """A label, the box, an error under the label when there is one, the explanation. `question`: the label is the
    screen's question (the sign-up). `reveal`: a Show/Hide button inside a password box (shown by the script).
    `error_ref`: the id of the problem's message shown elsewhere (a summary right above the field): the box is marked
    and described by it, and the message isn't repeated under the label."""
    help_id = f"{id}-help"
    error_id = f"{id}-error" if error and not error_ref else error_ref
    classes = "input" + (" mono" if mono else "") + (" code" if code else "")
    box = (f"<input class=\"{classes}\" id=\"{e(id)}\" name=\"{e(name or id)}\" type=\"{e(kind)}\" "
           f"aria-describedby=\"{e(described(error_id, help_id))}\""
           + (" aria-invalid=\"true\"" if error or error_ref else "")
           + (f" {attrs}" if attrs else "") + ">")
    if reveal:
        show, hide = e(SHOW + label), e(HIDE + label)
        box = (f"<div class=\"with-btn\">{box}<button class=\"reveal\" type=\"button\" data-reveal=\"{e(id)}\" "
               f"aria-controls=\"{e(id)}\" aria-label=\"{show}\" data-show=\"Show\" data-hide=\"Hide\" "
               f"data-show-label=\"{show}\" data-hide-label=\"{hide}\" hidden>Show</button></div>")
    label_html = (f"<label class=\"q\" for=\"{e(id)}\">{e(label)}</label>" if question
                  else f"<label for=\"{e(id)}\">{e(label)}</label>")
    error_html = f"<p class=\"field-error\" id=\"{e(error_id)}\">{e(error)}</p>" if error and not error_ref else ""
    help_html = f"<p class=\"help\" id=\"{e(help_id)}\">{e(help_text)}</p>" if help_text else ""
    if question:
        return label_html + error_html + box + help_html
    return f"<div class=\"field\">{label_html}{error_html}{box}{help_html}</div>"


def choice(id, title, help_text, name=None, checked=False, needs=None, value="on", error_id=None):
    """A choice row: the whole row is a checkbox; a ticked row takes the chosen blue. `needs`: the id of the choice it
    works only with (the script greys it out while that one is unticked). `error_id`: the id of the problem's message
    shown with it: the box is marked and described by it, read first. Inside the label only phrasing content: its
    words are spans the stylesheet lays out as blocks. The box is named by its title alone, so its explanation is read
    once, as its description; the whole row still ticks it. A row without an explanation (`help_text` empty)
    has none written, nor a description pointing at one, as in radio_row."""
    refs = described(error_id, f"{id}-help" if help_text else None)
    return (f"<label class=\"choice\"{f' data-needs=\"{e(needs)}\"' if needs else ''}><input type=\"checkbox\" "
            f"id=\"{e(id)}\" name=\"{e(name or id)}\" value=\"{e(value)}\"{' checked' if checked else ''} "
            f"aria-labelledby=\"{e(id)}-title\"" + (f" aria-describedby=\"{e(refs)}\"" if refs else "")
            + (" aria-invalid=\"true\"" if error_id else "")
            + f"><span><b id=\"{e(id)}-title\">{e(title)}</b>"
            + (f"<span id=\"{e(id)}-help\">{e(help_text)}</span>" if help_text else "") + "</span></label>")


def choices(*rows):
    return "<div class=\"choices\">" + "".join(rows) + "</div>"


ORDINALS = ("First", "Second", "Third", "Fourth")
REMOVE = "Remove: "   # a slot's remove button is named this and its language (the script names it again on a change)


def slot_id(place, name="lang"):
    """The id of the box or button in `place` (from 1) of a group named `name`: the language slots' (language_slots),
    a row of buttons' (segmented). A problem with the group links to the first one: the group can't take focus
."""
    return f"{name}-{place}"


def language_slots(languages, chosen, limit, labelled_by, help_id, error_id=None, name="lang", marked=False):
    """The language slots: one select per place, "First" to the limit, each listing every language
    ("None" after the first). The page shows every slot; the script shows the filled ones and "Add language" up to the
    limit (`data-max-languages`), greys out a language already chosen in another slot, and names each remove button
    by its slot's language (after the words in `data-remove-label`). `languages`: (name, code) pairs. A record holding
    more languages than the limit (a lowered limit) shows all of them. `marked`: the slots come with the hidden
    lang_ui field, by which signup.form_languages tells the public pages' form from an older one of typed names (the
    installer's form has no older one)."""
    chosen = [c for c in dict.fromkeys(chosen) if any(c == code for _, code in languages)]
    count = max(limit, len(chosen))
    rows = []
    for i in range(count):
        current = chosen[i] if i < len(chosen) else ("" if i else DEFAULT_LANGUAGE)
        options = ("" if i == 0 else f"<option value=\"\"{' selected' if not current else ''}>None</option>") + "".join(
            f"<option value=\"{e(code)}\"{' selected' if code == current else ''}>{e(lang)}</option>"
            for lang, code in languages)
        label = ORDINALS[i] if i < len(ORDINALS) else str(i + 1)
        current_name = next((lang for lang, code in languages if code == current), label)
        remove = ("<span></span>" if i == 0 else
                  f"<button class=\"tool\" type=\"button\" data-remove-slot aria-label=\"{e(REMOVE)}{e(current_name)}\" "
                  f"hidden>{CROSS}</button>")
        box = e(slot_id(i + 1, name))
        rows.append(f"<div class=\"slot\" data-slot><label for=\"{box}\">{e(label)}</label><div class=\"sel\">"
                    f"<select id=\"{box}\" name=\"{e(name)}\"{'' if current else ' class=\"empty\"'}>{options}"
                    f"</select>{CHEVRON}</div>{remove}</div>")
    return (("<input type=\"hidden\" name=\"lang_ui\" value=\"1\">" if marked else "")
            + f"<div class=\"slots\" id=\"languages\" role=\"group\" "
            f"aria-labelledby=\"{e(labelled_by)}\" aria-describedby=\"{e(described(error_id, help_id))}\" "
            f"data-max-languages=\"{int(limit)}\" data-remove-label=\"{e(REMOVE)}\">{''.join(rows)}</div>"
            f"<button class=\"btn secondary add\" type=\"button\" data-add hidden>{PLUS}Add language</button>")


# ------------------------------------------------------------------------------------------- notes, lists, the link

def note(kind, text=None, title=None, role=None, id=None, focus=False, extra=""):
    """A note after an action: done (ok), a warning (warn) or a problem: one surface, a mark, the words. `extra`: more
    HTML the caller built from escaped parts (a second paragraph, a list, a problem's link)."""
    icon = TICK if kind == "ok" else MARK
    attrs = ((f" role=\"{e(role)}\"" if role else "") + (f" id=\"{e(id)}\"" if id else "")
             + (" tabindex=\"-1\"" if focus else ""))
    head = f"<b>{e(title)}</b>" if title else ""
    words = f"<p>{e(text)}</p>" if text else ""
    return f"<div class=\"note {e(kind)}\"{attrs}>{icon}<div>{head}{words}{extra}</div></div>"


def problem_summary(error, title, field=None, extra=""):
    """The summary of a problem at the top of a page (the site's, the setup page's): its title and its message, a link
    to the field it is about (`field`: that field's id, when the page has it). The script focuses it. Nothing without
    an `error`."""
    if not error:
        return ""
    message = f"<a href=\"#{e(field)}\">{e(error)}</a>" if field else e(error)
    return note("problem", None, title, role="alert", id="problem", focus=True,
                extra=f"<p id=\"problem-message\">{message}</p>{extra}")


def dots(items):
    """A list with square dots; an item given as (text, [items]) has those listed under it."""
    def item(entry):
        text, under = entry if isinstance(entry, tuple) else (entry, ())
        return f"<li>{e(text)}{dots(under) if under else ''}</li>"
    return "<ul class=\"dots\">" + "".join(item(entry) for entry in items) + "</ul>"


def link_block(url, help_text):
    """The personal link in the accent block, with Copy link (shown by the script where copying works). The label's id
    is personal-l, for the block's aria-labelledby."""
    return ("<label id=\"personal-l\" for=\"personal\">Your personal addon link</label><textarea class=\"link\" id=\"personal\" "
            f"rows=\"3\" readonly spellcheck=\"false\" aria-describedby=\"personal-help\">{e(url)}</textarea>"
            f"<button class=\"btn light\" type=\"button\" data-copy=\"personal\" data-done=\"Copied\" hidden>{COPY}"
            f"<span>Copy link</span></button><span class=\"sr\" id=\"personal-status\" role=\"status\"></span>"
            f"<p class=\"help\" id=\"personal-help\">{e(help_text)}</p>")


def gauge(value, unit, share, full=False):
    """Today's use of one limit: the value in white, the unit in grey, a bar drawn in SVG (no inline style)."""
    width = f"{max(0.0, min(1.0, share)) * 100:.1f}%"
    return (f"<div class=\"gauge{' full' if full else ''}\"><p><b>{e(value)}</b><span>{e(unit)}</span></p>"
            f"<svg class=\"bar\" aria-hidden=\"true\" focusable=\"false\"><rect class=\"track\" width=\"100%\" height=\"10\" "
            f"rx=\"5\"/>" + (f"<rect class=\"fill\" width=\"{width}\" height=\"10\" rx=\"5\"/>" if share > 0 else "")
            + "</svg></div>")


# ------------------------------------------------------------------------------------------- the installer's parts

def radio_row(name, value, title, text, checked=False, note=None, invalid=False):
    """A way to choose, one of a few: the whole row is a radio button; the picked one takes the chosen blue. `text`:
    its words under the title (none when empty). `note`: words shown under them only while this row is the one
    picked; the stylesheet hides them otherwise, so it works without the script, and a browser that can't tell shows
    them. `invalid`: the answer has a problem (its group is described by the problem's message, radio_group). Inside
    the label only phrasing content, as in choice; the button is named by its title alone, as there."""
    rid = f"{name}-{value}"
    refs = described(f"{rid}-help" if text else None, f"{rid}-note" if note else None)
    return (f"<label class=\"way\"><input type=\"radio\" name=\"{e(name)}\" id=\"{e(rid)}\" value=\"{e(value)}\""
            f"{' checked' if checked else ''} aria-labelledby=\"{e(rid)}-title\""
            + (f" aria-describedby=\"{e(refs)}\"" if refs else "") + (" aria-invalid=\"true\"" if invalid else "")
            + f"><span><span class=\"head\" id=\"{e(rid)}-title\"><b>{e(title)}</b></span>"
            + (f"<span id=\"{e(rid)}-help\">{e(text)}</span>" if text else "")
            + (f"<span class=\"if-chosen\" id=\"{e(rid)}-note\">{e(note)}</span>" if note else "") + "</span></label>")


def radio_group(legend, rows, help_text=None, id=None, labelled_by=None, described_by=None, error_id=None):
    """One of a few ways, as radio rows under their question. `legend`: the question (None: `labelled_by`, the id of
    a heading that names the group, as a settings row's does); `help_text`: its explanation under the question, named
    "<id>-help"; `id`: the group's id (a problem's link goes to it); `described_by`: the id of an explanation shown
    elsewhere on the page; `error_id`: the id of the problem's message (its rows marked `invalid`), read first."""
    help_id = described_by or (f"{id}-help" if help_text and id else None)
    refs = described(error_id, help_id)
    attrs = ((f" id=\"{e(id)}\"" if id else "") + (f" aria-labelledby=\"{e(labelled_by)}\"" if labelled_by else "")
             + (f" aria-describedby=\"{e(refs)}\"" if refs else ""))
    return (f"<fieldset class=\"ways\"{attrs}>" + (f"<legend>{e(legend)}</legend>" if legend else "")
            + (f"<p class=\"help\" id=\"{e(help_id)}\">{e(help_text)}</p>" if help_text and help_id else "")
            + "<div class=\"options\">" + "".join(rows) + "</div></fieldset>")


def step_frame(steps, current, content, rail_label, done=False, back=None):
    """The installer's frame: a step list on the left on desktop; on phones and tablets, segments on top with the back
    arrow, and the step's Continue in thumb reach. `current`: the index of the step shown; `done`: every step done;
    `back`: the address the back arrow links to (none: no back arrow); a link, so the pages work without the script."""
    rail, segs = [], []
    for i, name in enumerate(steps):
        is_done, is_now = done or i < current, not done and i == current
        state = " class=\"done\"" if is_done else ""
        here = " aria-current=\"step\"" if is_now else ""
        rail.append(f"<li{state}{here}><span class=\"dot\">{TICK_BOLD if is_done else ''}</span>{e(name)}</li>")
        segs.append(f"<li{state}{here}></li>")
    back_html = f"<a class=\"icon-btn\" href=\"{e(back)}\" aria-label=\"Back\">{BACK}</a>" if back else ""
    return (f"<div class=\"frame\"><ol class=\"rail\" aria-label=\"{e(rail_label)}\">{''.join(rail)}</ol>"
            f"<main class=\"steparea\"><div class=\"fbar\">{back_html}<ol class=\"segs\" aria-hidden=\"true\">"
            f"{''.join(segs)}</ol></div>{content}</main></div>")


def _limit_error(id, error, help_text):
    """A limit's problem as field() shows one: the message under the label (`id`-error) and the attributes that mark
    the control as wrong and describe it by the message, read first, then by its explanation."""
    help_id = f"{id}-help" if help_text else None
    error_id = f"{id}-error" if error else None
    refs = described(error_id, help_id)
    attrs = (f" aria-describedby=\"{e(refs)}\"" if refs else "") + (" aria-invalid=\"true\"" if error else "")
    return (f"<p class=\"field-error\" id=\"{e(error_id)}\">{e(error)}</p>" if error else ""), attrs


def number_field(id, label, value, help_text=None, unit=None, keypad="numeric", error=None):
    """A number with its unit inside the box. `keypad`: the keyboard phones show (inputmode): numeric for a whole
    number, decimal for one with a decimal point, text for a box that also takes the word auto. No pattern: the value
    is checked where it is sent, and a pattern called a correct "10,000" invalid. `error`: the problem with
    the value, under the label, as field() shows it."""
    error_html, attrs = _limit_error(id, error, help_text)
    return (f"<div class=\"limit field\"><label for=\"{e(id)}\">{e(label)}</label>{error_html}<div class=\"num"
            f"{' has-unit' if unit else ''}\"><input id=\"{e(id)}\" name=\"{e(id)}\" type=\"text\" "
            f"inputmode=\"{e(keypad)}\" "
            f"value=\"{e(value)}\" autocomplete=\"off\" spellcheck=\"false\"{attrs}>"
            + (f"<span class=\"unit\">{e(unit)}</span>" if unit else "") + "</div>"
            + (f"<p class=\"help\" id=\"{e(id)}-help\">{e(help_text)}</p>" if help_text else "") + "</div>")


def segmented(name, label, options, value, help_text=None, unit=None, error=None):
    """One value of a few, as joined rectangles (radio buttons underneath); the group's id is `name`, its first button's
    slot_id(1, name), which a problem links to. `error`: the problem with the value, under the label and outside the
    row, as field() shows it."""
    first = f" id=\"{e(slot_id(1, name))}\""
    items = "".join(f"<label><input type=\"radio\"{first if i == 0 else ''} name=\"{e(name)}\" value=\"{e(v)}\""
                    f"{' checked' if str(v) == str(value) else ''}><span>{e(v)}</span></label>"
                    for i, v in enumerate(options))
    error_html, attrs = _limit_error(name, error, help_text)
    return (f"<div class=\"limit field\"><span class=\"label\" id=\"{e(name)}-label\">{e(label)}</span>{error_html}"
            f"<div class=\"seg-row\"><div class=\"seg\" id=\"{e(name)}\" role=\"radiogroup\" "
            f"aria-labelledby=\"{e(name)}-label\"{attrs}>{items}</div>"
            + (f"<span class=\"seg-unit\">{e(unit)}</span>" if unit else "") + "</div>"
            + (f"<p class=\"help\" id=\"{e(name)}-help\">{e(help_text)}</p>" if help_text else "") + "</div>")


def _marked_list(tag, cls, marks, rows):
    """A list whose rows are (kind, text, more): each marked by its kind's (icon, spoken name) in `marks`, `more` an
    optional line under the text (check_rows, progress_list)."""
    out = []
    for kind, text, more in rows:
        icon, spoken = marks[kind]
        out.append(f"<li class=\"{e(kind)}\"><span class=\"ic\" role=\"img\" aria-label=\"{e(spoken)}\">{icon}</span>"
                   f"<span class=\"tx\">{e(text)}" + (f"<span class=\"more\">{e(more)}</span>" if more else "")
                   + "</span></li>")
    return f"<{tag} class=\"{cls}\">" + "".join(out) + f"</{tag}>"


CHECK_MARKS = {"ok": (TICK_BOLD, "Done"), "warn": (MARK, "Warning"), "bad": (CROSS_BOLD, "Problem")}


def check_rows(rows):
    """Check rows: (kind, text, more) with kind ok, warn or bad; `more` an optional line under the text."""
    return _marked_list("ul", "checks", CHECK_MARKS, rows)


PROGRESS_MARKS = {"done": (TICK_BOLD, "Done"), "now": ("", "Working"), "next": ("", "Waiting")}


def progress_list(rows):
    """The install's progress: (state, text, more) with state done, now or next; `more` an optional line under the
    text, as in check_rows."""
    return _marked_list("ol", "progress", PROGRESS_MARKS, rows)


def config_changes(sections):
    """The list of changes to configs: (heading, [(sign, line)], note) per config; sign "+" or "−"; a note's paragraphs
    on lines of their own. Each heading is one level under the step's own."""
    out = []
    for heading, lines, after in sections:
        items = "".join(f"<li><span class=\"sign\" aria-hidden=\"true\">{e(sign)}</span>{e(line)}</li>" for sign, line in lines)
        notes = "".join(f"<p>{e(paragraph)}</p>" for paragraph in (after or "").split("\n") if paragraph)
        # a config with no change lines (subsync's own, an unchanged one) has no list: an empty one is a gap, and a
        # list of 0 items to a screen reader
        out.append(f"<section><h2>{e(heading)}</h2>" + (f"<ul>{items}</ul>" if lines else "") + f"{notes}</section>")
    return "<div class=\"changes\">" + "".join(out) + "</div>"


def row(hid, heading, about_html, controls_html):
    """A settings row (the settings page, the setup page): its heading and explanation on the left, its controls on the
    right (stacked on phones). `about_html` and `controls_html`: HTML the caller built from escaped parts."""
    return (f"<section class=\"grid12 row\" aria-labelledby=\"{e(hid)}\"><div class=\"about\"><h2 id=\"{e(hid)}\">"
            f"{e(heading)}</h2>{about_html}</div>{controls_html}</section>")


def update_panel(heading, about, running, ready, action, note=None):
    """The update panel: what updating does, the version running, the one ready (None: up to date), a `note`
    under them (why there is none to install, say), and the action. The heading's id is up-h, for the section's
    aria-labelledby."""
    lines = (f"<p>{e(running)}</p>" + (f"<p class=\"new\">{e(ready)}</p>" if ready else "")
             + (f"<p>{e(note)}</p>" if note else ""))
    actions = f"<div class=\"actions\">{button(action, type='submit')}</div>" if ready else ""
    return row("up-h", heading, f"<p class=\"help\">{e(about)}</p>",
               f"<div class=\"controls\"><div class=\"version\">{lines}</div>{actions}</div>")


def code_lines(lines):
    """Lines to copy as they are (a command, a web server's rule), in the mono face (the installer's pages)."""
    return "<pre class=\"code\">" + "\n".join(e(line) for line in lines) + "</pre>"
