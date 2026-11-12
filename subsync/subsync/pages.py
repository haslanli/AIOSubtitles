"""The public mode's web pages: sign-up, the result, a person's settings, delete, privacy. Plain HTML with the style
inline, no scripts and nothing loaded from elsewhere; every value from a person or a config is escaped. The texts come
from explain.py, where each has its one explanation."""
import html

from . import explain
from .aiosconfig import LANGUAGES

_NAMES = {three: name for name, _, three in LANGUAGES}

_STYLE = """
:root { color-scheme: light dark; --fg: #1d1d1f; --muted: #5f6368; --bg: #fbfbfa; --card: #ffffff; --line: #d9d9de;
        --accent: #1f5fbf; --warn-bg: #fff5e0; --warn-fg: #6b4500; --err-bg: #fdecea; --err-fg: #8a1c12; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #ececf1; --muted: #a6a6b0; --bg: #16171a; --card: #1f2024; --line: #36373d; --accent: #7fb0ff;
          --warn-bg: #3a2d10; --warn-fg: #f5d48a; --err-bg: #3d1714; --err-fg: #ffb4aa; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 system-ui, -apple-system, "Segoe UI",
       Roboto, sans-serif; }
main { max-width: 40rem; margin: 0 auto; padding: 2rem 1rem 3rem; }
h1 { font-size: 1.6rem; margin: 0 0 .5rem; } h2 { font-size: 1.15rem; margin: 2rem 0 .5rem; }
p { margin: .5rem 0; } .muted, .help { color: var(--muted); } .help { font-size: .9rem; margin: .25rem 0 1rem; }
form { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 1rem; margin: 1rem 0; }
label { display: block; font-weight: 600; margin-top: .5rem; }
input[type=text], input[type=url], input[type=password] { width: 100%; padding: .55rem .6rem; font: inherit;
  color: inherit; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
.check label { display: inline; font-weight: 600; } .check { margin-top: .75rem; }
button { margin-top: .75rem; padding: .6rem 1rem; font: inherit; font-weight: 600; color: #fff; background: var(--accent);
         border: 0; border-radius: 6px; cursor: pointer; }
.danger { background: #b3261e; }
code.link { display: block; padding: .6rem; overflow-wrap: anywhere; background: var(--card);
            border: 1px solid var(--line); border-radius: 6px; }
.warn { background: var(--warn-bg); color: var(--warn-fg); padding: .75rem; border-radius: 8px; }
.error { background: var(--err-bg); color: var(--err-fg); padding: .75rem; border-radius: 8px; }
footer { margin-top: 2.5rem; font-size: .9rem; } a { color: var(--accent); }
"""


def e(value):
    return html.escape(str(value), quote=True)


def _page(title, body):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{e(title)}</title><style>{_STYLE}</style></head><body><main>{body}"
            "<footer><a href=\"/privacy\">Privacy</a> · <a href=\"/\">AIOSubtitles</a></footer></main></body></html>")


def _help(key):
    return f"<p class=\"help\" id=\"{e(key)}-help\">{e(explain.PUBLIC_FIELDS[key])}</p>"


def _field(name, label, kind="text", value="", extra="", help=None):
    key = help or name   # the explanation (and the element id) when the field's name is shared, like "password"
    return (f"<label for=\"{e(key)}\">{e(label)}</label><input type=\"{kind}\" id=\"{e(key)}\" name=\"{e(name)}\" "
            f"value=\"{e(value)}\" aria-describedby=\"{e(key)}-help\" {extra}>{_help(key)}")


def _check(name, label, checked=True):
    return (f"<div class=\"check\"><input type=\"checkbox\" id=\"{e(name)}\" name=\"{e(name)}\" value=\"on\""
            f"{' checked' if checked else ''} aria-describedby=\"{e(name)}-help\"> <label for=\"{e(name)}\">{e(label)}"
            f"</label></div>{_help(name)}")


def _error(message):
    return f"<p class=\"error\" role=\"alert\">{e(message)}</p>" if message else ""


def language_names(codes):
    return ", ".join(_NAMES.get(c, c) for c in codes)


def signup_page(settings, error=None, languages="English"):
    invite = _field("invite", "Invite code", extra="required autocomplete=\"off\"") if settings.invite else ""
    body = (f"<h1>AIOSubtitles</h1><p>{e(explain.PUBLIC_TEXTS['intro'])}</p>{_error(error)}"
            "<form method=\"post\" action=\"/signup\">"
            + invite
            + _field("link", "Your AIOStreams link", "url", extra="required autocomplete=\"off\" spellcheck=\"false\" "
                     "placeholder=\"https://…/stremio/…/manifest.json\"")
            + _field("password", "Your AIOStreams password", "password", extra="required autocomplete=\"off\"")
            + _field("languages", "Languages", value=languages)
            + _check("add_addon", "Add AIOSubtitles to my AIOStreams config")
            + _check("add_opensubtitles", "Also add OpenSubtitles if my AIOStreams has no subtitle source")
            + "<button type=\"submit\">Sign up</button></form>")
    return _page("AIOSubtitles", body)


def _settings_link(manifest_url):
    return manifest_url[: -len("manifest.json")] + "configure"


def result_page(result):
    parts = [f"<h1>You're signed up</h1><p>{e(explain.PUBLIC_TEXTS['signed_up'])}</p>",
             f"<p><strong>Your personal addon link</strong></p><code class=\"link\">{e(result.manifest_url)}</code>"]
    if result.warning:
        parts.append(f"<p class=\"warn\">{e(result.warning)}</p>")
    if result.added:
        if result.changes:
            parts.append("<p>Changed in your AIOStreams config:</p><ul>"
                         + "".join(f"<li>{e(c)}</li>" for c in result.changes) + "</ul>")
        else:
            parts.append("<p>AIOSubtitles is in your AIOStreams config already.</p>")
    else:
        if result.manual:
            parts.append(f"<p>{e(explain.PUBLIC_TEXTS['not_added'])}</p>")
        parts.append(f"<p>{e(explain.PUBLIC_TEXTS['manual'])}</p>")
    parts.append(f"<p>Your settings page: <a href=\"{e(_settings_link(result.manifest_url))}\">open it</a> (it is "
                 "your personal link with /configure in place of /manifest.json).</p>")
    return _page("Signed up · AIOSubtitles", "".join(parts))


def _megabytes(nbytes):
    """A read amount: one decimal under 10 MB (at least 0.1 when anything was read), whole MB from 10 MB."""
    mb = nbytes / (1024 * 1024)
    if nbytes <= 0:
        return "0"
    return f"{max(mb, 0.1):.1f}" if mb < 10 else f"{mb:.0f}"


def settings_page(settings, record, usage, manifest_url, message=None, error=None):
    limit_mb = settings.user_daily_bytes / (1024 * 1024)
    body = ("<h1>Your AIOSubtitles</h1>"
            + (f"<p class=\"warn\" role=\"status\">{e(message)}</p>" if message else "") + _error(error)
            + f"<p><strong>Your personal addon link</strong></p><code class=\"link\">{e(manifest_url)}</code>"
            + f"<p class=\"muted\">Today: {usage['files']} of {settings.user_files_per_day} new files, "
              f"{_megabytes(usage['bytes'])} MB of {limit_mb:.0f} MB read. A limit reached means only copies already synced for "
              "someone are offered until tomorrow (UTC).</p>"
            + "<h2>Languages</h2><form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" "
              "value=\"languages\">" + _field("languages", "Languages", value=language_names(record["languages"]))
            + "<button type=\"submit\">Save languages</button></form>"
            + "<h2>Refresh</h2><form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" "
              "value=\"refresh\">" + _field("password", "Your AIOStreams password", "password",
                                            extra="required autocomplete=\"off\"", help="refresh")
            + "<input type=\"hidden\" name=\"add_addon\" value=\"on\"><button type=\"submit\">Refresh</button></form>"
            + "<h2>Delete my data</h2><form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" "
              f"value=\"delete-confirm\"><p class=\"help\">{e(explain.PUBLIC_FIELDS['delete'])}</p>"
              "<button type=\"submit\" class=\"danger\">Delete my data…</button></form>")
    return _page("Settings · AIOSubtitles", body)


def delete_confirm_page():
    body = ("<h1>Delete your data?</h1>"
            "<form method=\"post\" action=\"configure\"><input type=\"hidden\" name=\"action\" value=\"delete\">"
            + _field("password", "Your AIOStreams password (optional)", "password", extra="autocomplete=\"off\"",
                     help="delete")
            + "<button type=\"submit\" class=\"danger\">Delete my data</button></form>"
            "<p><a href=\"configure\">Keep it</a></p>")
    return _page("Delete · AIOSubtitles", body)


def deleted_page(outcome):
    parts = [f"<h1>Deleted</h1><p>{e(explain.PUBLIC_TEXTS['deleted'])}</p>"]
    if not outcome.get("sync_removed"):
        parts.append(f"<p class=\"warn\">{e(explain.PUBLIC_TEXTS['sync_left'])}</p>")
    if not outcome.get("addon_removed"):
        parts.append(f"<p>{e(explain.PUBLIC_TEXTS['addon_left'])}</p>")
    return _page("Deleted · AIOSubtitles", "".join(parts))


def privacy_page(settings):
    body = (f"<h1>Privacy</h1><p>{e(explain.PUBLIC_TEXTS['privacy'])}</p>"
            f"<p>Questions: <a href=\"{e(explain.CONTACT)}\">{e(explain.CONTACT)}</a></p>")
    return _page("Privacy · AIOSubtitles", body)


def error_page(message):
    return _page("AIOSubtitles", f"<h1>AIOSubtitles</h1>{_error(message)}<p><a href=\"/\">Back</a></p>")
