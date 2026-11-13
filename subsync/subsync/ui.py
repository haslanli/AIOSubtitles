"""The parts AIOSubtitles' pages are built from, and the parts the installer reuses.

Each part is a small function that returns HTML. Every value is escaped here; no part writes a style attribute (the
security header allows only static/app.css). The look is in static/app.css, the few moments that move in
static/app.js, and the words come from the caller (explain.py and pages.py hold the approved texts).
"""
import functools
import hashlib
import html
import os

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


# ------------------------------------------------------------------------------------------- the page frame

def page(title, body, kind, personal=False, body_attrs="", opened=False, refresh=None):
    """A whole page around `body` (which starts with top()). `kind` names the layout: start, done, settings, letter.
    Personal pages ask not to be indexed. `body_attrs`: more attributes of <body> (escaped by the caller); `opened`:
    the sign-up layer comes back open (after a problem); `refresh`: seconds after which the page loads itself again
    (the installer's progress, without the script)."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            + ("<meta name=\"robots\" content=\"noindex\">" if personal else "")
            + (f"<meta http-equiv=\"refresh\" content=\"{int(refresh)}\">" if refresh else "")
            + f"<title>{e(title)}</title><link rel=\"preload\" href=\"/static/fonts/{FONT_FILES[0]}\" as=\"font\" "
            f"type=\"font/ttf\" crossorigin><link rel=\"stylesheet\" href=\"{CSS_URL}\">"
            f"<script src=\"{JS_URL}\" defer></script></head><body class=\"p-{e(kind)}{' open' if opened else ''}\"{body_attrs}>"
            f"{body}</body></html>")


def top(contact, start="/", privacy=True):
    """The page's header: the name, Privacy (the site only) and Questions."""
    return (f"<header class=\"top\"><a class=\"wordmark\" href=\"{e(start)}\">AIOSubtitles</a>"
            f"<nav class=\"links\" aria-label=\"AIOSubtitles\">" + ("<a href=\"/privacy\">Privacy</a>" if privacy else "")
            + f"<a href=\"{e(contact)}\">Questions</a></nav></header>")


# ------------------------------------------------------------------------------------------- buttons

def button(label, kind="primary", type="button", attrs="", arrow=False, icon=""):
    return (f"<button class=\"btn {kind}\" type=\"{type}\"{(' ' + attrs) if attrs else ''}>{icon}{e(label)}"
            f"{ARROW if arrow else ''}</button>")


def link_button(label, href, kind="primary", attrs="", arrow=False):
    return (f"<a class=\"btn {kind}\" href=\"{e(href)}\"{(' ' + attrs) if attrs else ''}>{e(label)}"
            f"{ARROW if arrow else ''}</a>")


# ------------------------------------------------------------------------------------------- fields

def described(*ids):
    return " ".join(i for i in ids if i)


def field(id, label, help_text, name=None, kind="text", mono=False, error=None, attrs="", reveal=False, question=False,
          code=False, help_id=None, error_ref=None):
    """A label, the box, an error under the label when there is one, the explanation. `question`: the label is the
    screen's question (the sign-up). `reveal`: a Show/Hide button inside a password box (shown by the script).
    `error_ref`: the id of the problem's message shown elsewhere (a summary right above the field): the box is marked
    and described by it, and the message isn't repeated under the label."""
    help_id = help_id or f"{id}-help"
    error_id = f"{id}-error" if error and not error_ref else error_ref
    classes = "input" + (" mono" if mono else "") + (" code" if code else "")
    box = (f"<input class=\"{classes}\" id=\"{e(id)}\" name=\"{e(name or id)}\" type=\"{kind}\" "
           f"aria-describedby=\"{described(error_id, help_id)}\"" + (" aria-invalid=\"true\"" if error or error_ref else "")
           + (f" {attrs}" if attrs else "") + ">")
    if reveal:
        box = (f"<div class=\"with-btn\">{box}<button class=\"reveal\" type=\"button\" data-reveal=\"{e(id)}\" "
               f"aria-controls=\"{e(id)}\" data-show=\"Show\" data-hide=\"Hide\" hidden>Show</button></div>")
    label_html = (f"<label class=\"q\" for=\"{e(id)}\">{e(label)}</label>" if question
                  else f"<label for=\"{e(id)}\">{e(label)}</label>")
    error_html = f"<p class=\"field-error\" id=\"{e(error_id)}\">{e(error)}</p>" if error and not error_ref else ""
    help_html = f"<p class=\"help\" id=\"{e(help_id)}\">{e(help_text)}</p>" if help_text else ""
    if question:
        return label_html + error_html + box + help_html
    return f"<div class=\"field\">{label_html}{error_html}{box}{help_html}</div>"


def choice(id, title, help_text, name=None, checked=False, needs=None, value="on"):
    """A choice row: the whole row is a checkbox; a ticked row takes the chosen blue. `needs`: the id of the choice it
    works only with (the script greys it out while that one is unticked)."""
    return (f"<label class=\"choice\"{f' data-needs=\"{e(needs)}\"' if needs else ''}><input type=\"checkbox\" "
            f"id=\"{e(id)}\" name=\"{e(name or id)}\" value=\"{e(value)}\"{' checked' if checked else ''} "
            f"aria-describedby=\"{e(id)}-help\"><div><b>{e(title)}</b><span id=\"{e(id)}-help\">{e(help_text)}</span>"
            "</div></label>")


def choices(*rows):
    return "<div class=\"choices\">" + "".join(rows) + "</div>"


ORDINALS = ("First", "Second", "Third", "Fourth")


def language_slots(languages, chosen, limit, labelled_by, help_id, error_id=None, name="lang"):
    """The language slots: one select per place, "First" to the limit, each listing every language
    ("None" after the first). The page shows every slot; the script shows the filled ones and "Add language" up to the
    limit (`data-max-languages`), and greys out a language already chosen in another slot. `languages`: (name, code)
    pairs. A record holding more languages than the limit (a lowered limit) shows all of them."""
    chosen = [c for c in dict.fromkeys(chosen) if any(c == code for _, code in languages)]
    count = max(limit, len(chosen))
    rows = []
    for i in range(count):
        current = chosen[i] if i < len(chosen) else ("" if i else "eng")
        options = ("" if i == 0 else f"<option value=\"\"{' selected' if not current else ''}>None</option>") + "".join(
            f"<option value=\"{e(code)}\"{' selected' if code == current else ''}>{e(lang)}</option>"
            for lang, code in languages)
        label = ORDINALS[i] if i < len(ORDINALS) else str(i + 1)
        current_name = next((lang for lang, code in languages if code == current), label)
        remove = ("<span></span>" if i == 0 else
                  f"<button class=\"tool\" type=\"button\" data-remove-slot aria-label=\"Remove: {e(current_name)}\" "
                  f"hidden>{CROSS}</button>")
        rows.append(f"<div class=\"slot\" data-slot><label for=\"{e(name)}-{i + 1}\">{e(label)}</label><div class=\"sel\">"
                    f"<select id=\"{e(name)}-{i + 1}\" name=\"{e(name)}\"{'' if current else ' class=\"empty\"'}>{options}"
                    f"</select>{CHEVRON}</div>{remove}</div>")
    return (f"<input type=\"hidden\" name=\"lang_ui\" value=\"1\"><div class=\"slots\" id=\"languages\" role=\"group\" "
            f"aria-labelledby=\"{e(labelled_by)}\" aria-describedby=\"{described(error_id, help_id)}\" "
            f"data-max-languages=\"{int(limit)}\">{''.join(rows)}</div>"
            f"<button class=\"btn secondary add\" type=\"button\" data-add hidden>{PLUS}Add language</button>")


# ------------------------------------------------------------------------------------------- notes, lists, the link

def note(kind, text=None, title=None, role=None, id=None, focus=False, extra=""):
    """A note after an action: done (ok), a warning (warn) or a problem: one surface, a mark, the words. `extra`: more
    HTML the caller built from escaped parts (a second paragraph, a list, a problem's link)."""
    icon = TICK if kind == "ok" else MARK
    attrs = (f" role=\"{role}\"" if role else "") + (f" id=\"{e(id)}\"" if id else "") + (" tabindex=\"-1\"" if focus else "")
    head = f"<b>{e(title)}</b>" if title else ""
    words = f"<p>{e(text)}</p>" if text else ""
    return f"<div class=\"note {kind}\"{attrs}>{icon}<div>{head}{words}{extra}</div></div>"


def dots(items):
    return "<ul class=\"dots\">" + "".join(f"<li>{e(item)}</li>" for item in items) + "</ul>"


def link_block(url, help_text, label="Your personal addon link"):
    """The personal link in the accent block, with Copy link (shown by the script where copying works). The label's id
    is personal-l, for the block's aria-labelledby."""
    return (f"<label id=\"personal-l\" for=\"personal\">{e(label)}</label><textarea class=\"link\" id=\"personal\" rows=\"3\" readonly "
            f"spellcheck=\"false\" aria-describedby=\"personal-help\">{e(url)}</textarea>"
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

def radio_row(name, value, title, text, checked=False, tag=None):
    """A way to choose, one of a few: the whole row is a radio button; the picked one takes the chosen blue."""
    rid = f"{name}-{value}"
    return (f"<label class=\"way\"><input type=\"radio\" name=\"{e(name)}\" id=\"{e(rid)}\" value=\"{e(value)}\""
            f"{' checked' if checked else ''} aria-describedby=\"{e(rid)}-help\"><div><div class=\"head\"><b>{e(title)}</b>"
            + (f"<i>{e(tag)}</i>" if tag else "") + f"</div><span id=\"{e(rid)}-help\">{e(text)}</span></div></label>")


def radio_group(legend, rows):
    return (f"<fieldset class=\"ways\"><legend>{e(legend)}</legend><div class=\"options\">" + "".join(rows)
            + "</div></fieldset>")


def step_frame(steps, current, content, rail_label, done=False, back=True):
    """The installer's frame: a step list on the left on desktop; on phones and tablets, segments on top with the back
    arrow, and the step's Continue in thumb reach. `current`: the index of the step shown; `done`: every step done;
    `back`: the back arrow, a link when it is an address (the installer's pages work without the script)."""
    rail, segs = [], []
    for i, name in enumerate(steps):
        is_done, is_now = done or i < current, not done and i == current
        state = " class=\"done\"" if is_done else ""
        here = " aria-current=\"step\"" if is_now else ""
        rail.append(f"<li{state}{here}><span class=\"dot\">{TICK_BOLD if is_done else ''}</span>{e(name)}</li>")
        segs.append(f"<li{state}{here}></li>")
    if isinstance(back, str):
        back_html = f"<a class=\"icon-btn\" href=\"{e(back)}\" aria-label=\"Back\">{BACK}</a>"
    else:
        back_html = (f"<button class=\"icon-btn\" type=\"button\" data-back aria-label=\"Back\">{BACK}</button>" if back else "")
    return (f"<div class=\"frame\"><ol class=\"rail\" aria-label=\"{e(rail_label)}\">{''.join(rail)}</ol>"
            f"<main class=\"steparea\"><div class=\"fbar\">{back_html}<ol class=\"segs\" aria-hidden=\"true\">"
            f"{''.join(segs)}</ol></div>{content}</main></div>")


def number_field(id, label, value, help_text=None, unit=None, auto=False):
    """A number with its unit inside the box. `auto`: the box also takes the word auto."""
    pattern = "auto|[0-9]+" if auto else "[0-9]+"
    mode = "text" if auto else "numeric"
    return (f"<div class=\"limit field\"><label for=\"{e(id)}\">{e(label)}</label><div class=\"num"
            f"{' has-unit' if unit else ''}\"><input id=\"{e(id)}\" name=\"{e(id)}\" type=\"text\" inputmode=\"{mode}\" "
            f"pattern=\"{pattern}\" value=\"{e(value)}\" autocomplete=\"off\" spellcheck=\"false\""
            + (f" aria-describedby=\"{e(id)}-help\"" if help_text else "") + ">"
            + (f"<span class=\"unit\">{e(unit)}</span>" if unit else "") + "</div>"
            + (f"<p class=\"help\" id=\"{e(id)}-help\">{e(help_text)}</p>" if help_text else "") + "</div>")


def segmented(name, label, options, value, help_text=None, unit=None):
    """One value of a few, as joined rectangles (radio buttons underneath)."""
    items = "".join(f"<label><input type=\"radio\" name=\"{e(name)}\" value=\"{e(v)}\"{' checked' if str(v) == str(value) else ''}>"
                    f"<span>{e(v)}</span></label>" for v in options)
    return (f"<div class=\"limit field\"><span class=\"label\" id=\"{e(name)}-label\">{e(label)}</span><div class=\"seg-row\">"
            f"<div class=\"seg\" role=\"radiogroup\" aria-labelledby=\"{e(name)}-label\""
            + (f" aria-describedby=\"{e(name)}-help\"" if help_text else "") + f">{items}</div>"
            + (f"<span class=\"seg-unit\">{e(unit)}</span>" if unit else "") + "</div>"
            + (f"<p class=\"help\" id=\"{e(name)}-help\">{e(help_text)}</p>" if help_text else "") + "</div>")


CHECK_MARKS = {"ok": (TICK_BOLD, "Done"), "warn": (MARK, "Warning"), "bad": (CROSS_BOLD, "Problem")}


def check_rows(rows):
    """Check rows: (kind, text, more) with kind ok, warn or bad; `more` an optional line under the text."""
    out = []
    for kind, text, more in rows:
        icon, spoken = CHECK_MARKS[kind]
        out.append(f"<li class=\"{kind}\"><span class=\"ic\" role=\"img\" aria-label=\"{spoken}\">{icon}</span>"
                   f"<span class=\"tx\">{e(text)}" + (f"<span class=\"more\">{e(more)}</span>" if more else "")
                   + "</span></li>")
    return "<ul class=\"checks\">" + "".join(out) + "</ul>"


PROGRESS_MARKS = {"done": (TICK_BOLD, "Done"), "now": ("", "Working"), "next": ("", "Waiting")}


def progress_list(rows):
    """The install's progress: (state, text) with state done, now or next."""
    out = []
    for state, text in rows:
        icon, spoken = PROGRESS_MARKS[state]
        out.append(f"<li class=\"{state}\"><span class=\"ic\" role=\"img\" aria-label=\"{spoken}\">{icon}</span>"
                   f"<span class=\"tx\">{e(text)}</span></li>")
    return "<ol class=\"progress\">" + "".join(out) + "</ol>"


def config_changes(sections):
    """The list of changes to configs: (heading, [(sign, line)], note) per config; sign "+" or "−"."""
    out = []
    for heading, lines, after in sections:
        items = "".join(f"<li><span class=\"sign\" aria-hidden=\"true\">{e(sign)}</span>{e(line)}</li>" for sign, line in lines)
        out.append(f"<section><h3>{e(heading)}</h3><ul>{items}</ul>" + (f"<p>{e(after)}</p>" if after else "")
                   + "</section>")
    return "<div class=\"changes\">" + "".join(out) + "</div>"


def update_panel(heading, about, running_html, ready, action, hid="up-h"):
    """The update panel: what updating does, the version running (HTML the caller built from escaped parts),
    the one ready (None: up to date), and the action."""
    lines = f"<p>{running_html}</p>" + (f"<p class=\"new\">{e(ready)}</p>" if ready else "")
    return (f"<section class=\"grid12 row\" aria-labelledby=\"{e(hid)}\"><div class=\"about\"><h2 id=\"{e(hid)}\">"
            f"{e(heading)}</h2><p class=\"help\">{e(about)}</p></div><div class=\"controls\"><div class=\"version\">{lines}"
            "</div>" + (f"<div class=\"actions\">{button(action, type='submit')}</div>" if ready else "") + "</div></section>")


def code_lines(lines):
    """Lines to copy as they are (a command, a web server's rule), in the mono face (the installer's pages)."""
    return "<pre class=\"code\">" + "\n".join(e(line) for line in lines) + "</pre>"
