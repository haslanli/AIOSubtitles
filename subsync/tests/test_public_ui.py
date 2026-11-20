"""Public mode's pages: the look (the approved tokens, contrast, motion, the breakpoints, two typefaces, no
outside resources and no inline style), each page's structure, every text an approved one, errors tied to their field,
the language limit, stored copies only, the small script, the installer's parts, and the routes around the pages
(redirect after a save, the not-found page, noindex, the security header, the stylesheet, script and fonts)."""
import html.parser
import os
import re
import unittest

# helpers first: it puts the package on the import path
from helpers import (BASE, change_lines, form, HAVE_CRYPTOGRAPHY, http, INSTANCE, LINK, NEEDS, PASSWORD, RECORD,
                     result, ServerHarness, settings, TOKEN, Tree, USAGE, VIEWER)


def contrast(a, b):
    def lum(h):
        r, g, b_ = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b_)
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def css_rule(selector):
    """The declarations of the stylesheet's rule for `selector`, written at the start of its own line."""
    from subsync.ui import APP_CSS
    return re.search(r"(?m)^" + re.escape(selector) + r"\{([^}]*)\}", APP_CSS).group(1)


def css_media(query):
    """The rules inside the stylesheet's @media block for `query`."""
    from subsync.ui import APP_CSS
    start = APP_CSS.index(f"@media {query}{{") + len(f"@media {query}{{")
    depth, end = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(APP_CSS[end], 0)
        end += 1
    return APP_CSS[start:end - 1]


# The signed-up page's lines for the changes to a person's config, as the owner approved them
CHANGES = ("Adds 1 subtitle source: AIOSubtitles (synced copies of the subtitles you already get)",
           "Removes 2 subtitle addons AIOSubtitles now searches itself: SubDL, OpenSubtitles",
           "Updates the subtitle source \"AIOSubtitles\" (new address)",
           "Moves \"AIOSubtitles\" before your other subtitle sources (apps that list only a few subtitles per language "
           "keep its entries)")


CONTACT = "mailto:host@example.com"
# a host's own setup on its pages: its Questions link, the service in front, how long its backups keep records
HOST = {"SUBSYNC_CONTACT": CONTACT, "SUBSYNC_FRONT_PROXY": "Cloudflare", "SUBSYNC_BACKUP_DAYS": "30"}


def privacy_parts(s):
    """The privacy page's parts as the page fills them in: with the subtitle sites the instance asks itself."""
    from subsync import explain
    from subsync.extra import asked_sources
    return explain.privacy_parts(s, asked_sources(s, "series"))


HOSTILE = "<b onclick=\"x\">&'\""
BOXES = ("add_addon", "clean_up", "usenet", "keep_password")         # the sign-up's boxes (pages.SIGNUP_BOXES)
# a host allowing thousands: its settings, a record with Usenet on, and a day's use
THOUSANDS = (settings(SUBSYNC_USER_FILES_PER_DAY="2000", SUBSYNC_USENET="on", SUBSYNC_USENET_LISTINGS_PER_DAY="1500"),
             dict(RECORD, usenet_uuid="u"), {"files": 1200, "bytes": 9500 * 1024 * 1024})


def every_page():
    """Every page and state the build checks, rendered with made-up values."""
    from subsync import explain, pages
    s = settings(SUBSYNC_PUBLIC_INVITE="code")
    plain = settings()
    out = {
        "signup": pages.signup_page(s),
        "signup-no-invite": pages.signup_page(plain),
        "signup-limit-3": pages.signup_page(settings(SUBSYNC_MAX_LANGUAGES="3")),
        "signup-limit-1": pages.signup_page(settings(SUBSYNC_MAX_LANGUAGES="1")),
        "signup-error-invite": pages.signup_page(s, error="Wrong.", field="invite"),
        "signup-error-link": pages.signup_page(s, error="Wrong.", field="link"),
        "signup-error-password": pages.signup_page(s, error="Wrong.", field="password", order=["spa", "eng"]),
        "signup-error-languages": pages.signup_page(s, error="Wrong.", field="languages"),
        "signup-error-none": pages.signup_page(s, error="Wrong."),
        # Every box as the person left it; a problem where trying again only costs a try
        "signup-error-ticked": pages.signup_page(s, error="Wrong.", field="password",
                                                 ticked=dict.fromkeys(BOXES, True)),
        "signup-error-full": pages.signup_page(s, error="Wrong.", retry=False),
        "result": pages.result_page(result()),
        "result-manual": pages.result_page(result(added=False, manual=True, changes=[])),
        "result-unticked": pages.result_page(result(added=False, manual=False, changes=[])),
        "result-rd": pages.result_page(result(warning=explain.PUBLIC_REAL_DEBRID_WARNING)),
        "result-stored": pages.result_page(result(store_only=True)),
        "result-again": pages.result_page(result(changes=change_lines()[2:])),
        "settings": pages.settings_page(s, RECORD, USAGE, LINK, now=0),
        "settings-kept": pages.settings_page(s, dict(RECORD, auto_password="x"), USAGE, LINK, now=0),
        "settings-full": pages.settings_page(s, RECORD, {"files": 40, "bytes": 0}, LINK, now=0),
        "settings-saved": pages.settings_page(s, RECORD, USAGE, LINK, now=0,
                                              message=pages.done_message("languages", RECORD)[0]),
        "settings-error-refresh": pages.settings_page(s, RECORD, USAGE, LINK, now=0, error="Wrong.", field="refresh"),
        "settings-error-link": pages.settings_page(s, RECORD, USAGE, LINK, now=0, error="Wrong.", field="new_link"),
        "settings-error-languages": pages.settings_page(s, RECORD, USAGE, LINK, now=0, error="Wrong.", field="languages"),
        "settings-stored": pages.settings_page(s, dict(RECORD, store_only=True), USAGE, LINK, now=0),
        "settings-five": pages.settings_page(settings(SUBSYNC_MAX_LANGUAGES="2"),
                                             dict(RECORD, languages=["eng", "spa", "fre"]), USAGE, LINK, now=0),
        "settings-thousands": pages.settings_page(settings(SUBSYNC_USER_FILES_PER_DAY="2000"), RECORD, THOUSANDS[2],
                                                  LINK, now=0),
        "delete": pages.delete_confirm_page(),
        "delete-error": pages.delete_confirm_page(error="Wrong.", field="delete"),
        "delete-stored": pages.delete_confirm_page(stored=True),
        "deleted": pages.deleted_page({"sync_removed": True, "addon_removed": True}),
        "deleted-left": pages.deleted_page({}),
        "privacy": pages.privacy_page(s),
        # TMDB (the original-language lookup): its credit where the instance uses it, a person's own key's row
        "signup-tmdb": pages.signup_page(settings(SUBSYNC_TMDB_KEY="k")),
        "privacy-tmdb": pages.privacy_page(settings(SUBSYNC_TMDB_KEY="k")),
        "privacy-tmdb-personal": pages.privacy_page(settings(SUBSYNC_TMDB_PERSONAL="on")),
        "privacy-lookup-off": pages.privacy_page(settings(SUBSYNC_ORIGINAL_LANGUAGE="off")),
        "settings-tmdb-personal": pages.settings_page(settings(SUBSYNC_TMDB_PERSONAL="on"), RECORD, USAGE, LINK, now=0),
        "settings-tmdb-kept": pages.settings_page(settings(SUBSYNC_TMDB_PERSONAL="on"), dict(RECORD, tmdb_key="x" * 32),
                                                  USAGE, LINK, now=0, message=explain.PUBLIC_TEXTS["tmdb_saved"]),
        "settings-tmdb-error": pages.settings_page(settings(SUBSYNC_TMDB_PERSONAL="on"), RECORD, USAGE, LINK, now=0,
                                                   error=explain.PUBLIC_TEXTS["tmdb_refused"], field="tmdb_key"),
        # an instance with the audio route on: the pages say what it reads
        "signup-audio": pages.signup_page(settings(SUBSYNC_AUDIO_ROUTE="on")),
        "result-audio": pages.result_page(result(), settings(SUBSYNC_AUDIO_ROUTE="on")),
        "privacy-audio": pages.privacy_page(settings(SUBSYNC_AUDIO_ROUTE="on")),
        # each person's answer for files with no subtitles of their own: three answers, or two while the
        # instance allows no opened syncs; after a problem; on the settings page and after a save
        "signup-audio-two": pages.signup_page(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_USER_OPENED_PER_DAY="0")),
        "signup-audio-error": pages.signup_page(settings(SUBSYNC_AUDIO_ROUTE="on"), error="Wrong.", field="audio",
                                                audio="open"),
        "settings-audio": pages.settings_page(settings(SUBSYNC_AUDIO_ROUTE="on"), dict(RECORD, audio="open"), USAGE,
                                              LINK, now=0),
        "settings-audio-two": pages.settings_page(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_USER_OPENED_PER_DAY="0"),
                                                  RECORD, USAGE, LINK, now=0),
        "settings-audio-saved": pages.settings_page(settings(SUBSYNC_AUDIO_ROUTE="on"), dict(RECORD, audio="off"),
                                                    USAGE, LINK, now=0,
                                                    message=pages.done_message("audio", dict(RECORD, audio="off"))[0]),
        "settings-audio-error": pages.settings_page(settings(SUBSYNC_AUDIO_ROUTE="on"), RECORD, USAGE, LINK, now=0,
                                                    error="Wrong.", field="audio"),
        # No audio limit, and no audio syncs a day (the pages then treat the audio route as off)
        "privacy-audio-no-limit": pages.privacy_page(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_AUDIO_BUDGET_GB="0")),
        "signup-audio-no-limit": pages.signup_page(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_AUDIO_BUDGET_GB="0")),
        "signup-no-audio-syncs": pages.signup_page(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_USER_AUDIO_PER_DAY="0")),
        # The check off (no password kept), or every 6 hours
        "signup-no-check": pages.signup_page(settings(SUBSYNC_RECHECK_HOURS="0")),
        "settings-kept-no-check": pages.settings_page(settings(SUBSYNC_RECHECK_HOURS="0"),
                                                      dict(RECORD, auto_password="x"), USAGE, LINK, now=0),
        "settings-kept-6-hours": pages.settings_page(settings(SUBSYNC_RECHECK_HOURS="6"),
                                                     dict(RECORD, auto_password="x"), USAGE, LINK, now=0),
        # A host's own setup on every page
        "signup-host": pages.signup_page(settings(**HOST)),
        "result-host": pages.result_page(result(), settings(**HOST)),
        "settings-host": pages.settings_page(settings(SUBSYNC_TMDB_PERSONAL="on", **HOST),
                                             dict(RECORD, auto_password="x", tmdb_key="x" * 32), USAGE, LINK, now=0),
        "privacy-host": pages.privacy_page(settings(**HOST)),
        "delete-host": pages.delete_confirm_page(settings=settings(**HOST)),
        "deleted-host": pages.deleted_page({}, settings(**HOST)),
        "not-found-host": pages.not_found_page(settings(**HOST)),
        "not-found": pages.not_found_page(),
        "error": pages.error_page("Wrong."),
        # The page a browser gets after an unexpected failure, and under a personal link
        "error-unexpected": pages.error_page(settings=s),
        "error-personal": pages.error_page(settings=s, personal=True),
    }
    for code in pages.DONE:
        out[f"settings-done-{code}"] = pages.settings_page(s, RECORD, USAGE, LINK, now=0,
                                                           message=pages.done_message(code, RECORD)[0],
                                                           notice=pages.done_message(code, RECORD)[1])
    for state in explain.PUBLIC_STATES:
        out[f"settings-state-{state}"] = pages.settings_page(s, dict(RECORD, state=state, state_detail="Detail."),
                                                             USAGE, LINK, now=0)
    return out


def approved_texts():
    """Every approved text the pages may show, formatted with the made-up values every_page uses."""
    from subsync import explain, pages
    from subsync.aiosconfig import LANGUAGES
    texts = list(explain.PUBLIC_FIELDS.values()) + list(explain.PUBLIC_BEFORE) + list(explain.PUBLIC_MANUAL_STEPS)
    texts += list(explain.PUBLIC_DELETE_LIST.values()) + list(explain.PUBLIC_STORE_ONLY) + list(explain.PUBLIC_HEADLINE)
    texts += list(explain.PUBLIC_REAL_DEBRID_WARNING)       # its title and text apart (the result page); joined below
    texts += [explain.PUBLIC_REAL_DEBRID_NOTICE, explain.CONTACT, CONTACT]
    texts += [explain.languages_help(settings(SUBSYNC_MAX_LANGUAGES=str(n))) for n in (1, 2, 3, 4)]
    texts.append(explain.PUBLIC_MANUAL_LEAD)
    # the clean-up box's words with the addons each instance takes out: the OpenSubtitles ones, and all four
    from subsync.extra import cleanable_presets
    texts += [explain.clean_up_help(cleanable_presets(settings(**env)))
              for env in ({}, {"SUBSYNC_SUBDL_KEY": "k", "SUBSYNC_SUBSOURCE_KEY": "k"})]
    texts += [h for h, _ in explain.PRIVACY_PARTS]
    texts += [(t[:1].upper() + t[1:]) for _, t in explain.PRIVACY_PARTS]
    audio = settings(SUBSYNC_AUDIO_ROUTE="on")            # the audio route's words, as an instance with it on shows them
    texts += list(explain.intro_parts(audio)) + [explain.next_text(audio)]
    texts += [(t[:1].upper() + t[1:]) for _, t in privacy_parts(audio)]
    # without an audio limit, and with a host's own setup
    for s in (settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_AUDIO_BUDGET_GB="0"), settings(**HOST)):
        texts += [(t[:1].upper() + t[1:]) for _, t in privacy_parts(s)]
    texts.append(explain.audio_help(settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_AUDIO_BUDGET_GB="0")))
    # the fields that state how often the check runs and how long backups keep what is deleted
    for s in (settings(), settings(SUBSYNC_RECHECK_HOURS="6"), settings(**HOST)):
        texts += [explain.field_help(name, s) for name in ("tmdb_key", "keep_password", "forget_password")]
    # the original-language lookups' words, by what the instance uses (TMDB with its key, each person's, Wikidata
    # only, none), and TMDB's notice and its logo's name
    for env in ({"SUBSYNC_TMDB_KEY": "k"}, {"SUBSYNC_TMDB_PERSONAL": "on"}, {}, {"SUBSYNC_ORIGINAL_LANGUAGE": "off"},
                {"SUBSYNC_TMDB_KEY": "k", "SUBSYNC_AUDIO_ROUTE": "on"}):
        texts += [(t[:1].upper() + t[1:]) for _, t in privacy_parts(settings(**env))]
    texts.append(explain.TMDB_NOTICE)
    for title, text, _ in explain.PUBLIC_STATES.values():
        texts += [title, text.replace("{detail}", "Detail.")]
    for key, text in explain.PUBLIC_TEXTS.items():
        texts.append(text.format(when="at sign-up", names="English, Spanish", answer="Off",
                                 attempts="5 sign-up attempts", every="about once a day") if "{" in text else text)
    texts.append(explain.PUBLIC_TEXTS["copied_auto"].format(when="at sign-up", every="about every 6 hours"))
    # the answer for files with no subtitles of their own, as an instance with three or two answers shows it
    for env in ({}, {"SUBSYNC_USER_OPENED_PER_DAY": "0"}):
        offers = settings(SUBSYNC_AUDIO_ROUTE="on", **env)
        texts.append(explain.audio_help(offers))
        texts += [explain.audio_answer_help(value, offers) for value, _ in pages.audio_answers(offers)]
    texts.append(explain.PUBLIC_AUDIO["opened_note"])
    texts += [pages.done_message("audio", {"audio": value})[0] for value in ("off", "pick", "open")]
    texts.append(explain.PUBLIC_TEXTS["reset"].format(when=pages.reset_in(0)))
    # the last sign-up step's note, as today's page has it, the Privacy link after it
    texts.append(explain.PUBLIC_TEXTS["password_once"] + " What AIOSubtitles reads and keeps:")
    for lead in ("not_added", "refresh_manual"):          # a note's text with the manual steps after it
        texts.append(" ".join((explain.PUBLIC_TEXTS[lead],) + explain.PUBLIC_MANUAL_STEPS))
    texts += [name for name, _, _ in LANGUAGES] + list(pages.UI_WORDS) + list(CHANGES)
    return texts


def approved_corpus():
    """The approved texts in one string."""
    return "\n".join(approved_texts())


def split_pieces():
    """The pieces the pages' inline markup cuts out of an approved text, each shown on its own: the headline's last part
    either side of its line break (pages._headline), and the full stop after the Privacy link."""
    from subsync import explain
    word, _, rest = explain.PUBLIC_HEADLINE[-1].partition(" ")
    return {word, rest, "."}


COUNT = r"\d{1,3}(?:,\d{3})*"      # a figure with thousands separators, as the settings page writes it
DYNAMIC = re.compile(rf"^({COUNT} of {COUNT}|(?:\d\.\d|{COUNT}) MB of {COUNT} MB|English, Spanish(, French)?|Wrong\."
                     r"|Detail\.|https://subs\.test/\S*|Remove: \w+)$")


class InLabels(html.parser.HTMLParser):
    """The elements inside a label that aren't phrasing content (HTML: a label holds phrasing content only)."""
    FLOW = {"address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset", "figure",
            "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
            "pre", "section", "table", "ul"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.depth, self.flow = 0, []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if self.depth and tag in self.FLOW:
            self.flow.append(tag)
        if tag == "label":
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "label":
            self.depth -= 1


class Visible(html.parser.HTMLParser):
    """The text people see or hear: text outside script and style, button and link names, aria-labels, titles."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.skip, self.found = 0, []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        for name in ("aria-label", "placeholder", "data-show", "data-hide", "data-show-label", "data-hide-label",
                     "data-done", "data-remove-label"):
            if a.get(name):
                self.found.append(a[name])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data).strip()
        if text and not self.skip:
            self.found.append(text)


class LookTest(unittest.TestCase):
    TOKENS = {"ground": "#080808", "surface": "#141414", "field": "#0B0B0B", "row": "#1B1B1B", "edge": "#6E6E6E",
              "text": "#F5F5F5", "text-2": "#BDBDBD", "text-3": "#9E9E9E", "accent": "#378ADD",
              "accent-link": "#85B7EB", "accent-text": "#B5D4F4", "accent-bg": "#0C447C", "accent-bg-2": "#09365F",
              "on-accent-bg": "#D4E3F3", "danger": "#E24B4A", "danger-text": "#F7C1C1", "warn-text": "#FAC775"}

    def test_the_tokens_are_the_approved_ones(self):
        from subsync.ui import APP_CSS
        for name, value in self.TOKENS.items():
            with self.subTest(name=name):
                self.assertIn(f"--{name}:{value};", APP_CSS)

    def test_every_text_and_edge_pair_passes(self):
        t = self.TOKENS
        texts = [("text", "ground"), ("text", "surface"), ("text-2", "ground"), ("text-2", "surface"), ("text-2", "row"),
                 ("text-3", "ground"), ("text-3", "surface"), ("text-3", "row"), ("accent-link", "ground"),
                 ("accent-link", "surface"), ("text", "accent-bg"), ("on-accent-bg", "accent-bg"),
                 ("text", "accent-bg-2"), ("accent-text", "accent-bg"), ("danger-text", "ground"),
                 ("danger-text", "surface"), ("warn-text", "surface"), ("text-3", "field")]
        for fg, bg in texts:
            with self.subTest(fg=fg, bg=bg):
                self.assertGreaterEqual(contrast(t[fg], t[bg]), 4.5)
        # the primary button's label: white, 19 px bold (large text); edges and focus rings at least 3:1
        self.assertGreaterEqual(contrast("#FFFFFF", t["accent"]), 3.0)
        for fg, bg in (("edge", "surface"), ("edge", "ground"), ("edge", "field"), ("accent-link", "accent-bg"),
                       ("danger", "ground"), ("danger", "surface")):
            with self.subTest(edge=fg, bg=bg):
                self.assertGreaterEqual(contrast(t[fg], t[bg]), 3.0)

    def test_a_colour_a_token_holds_is_written_as_the_token(self):
        # --accent-bg's #0C447C (five times) and --surface's #141414 were written out after the token block,
        # where a change of the token wouldn't reach them
        from subsync.ui import APP_CSS
        tokens, _, rules = APP_CSS.partition(":root{")[2].partition("}")
        held = {value.upper() for value in re.findall(r"--[\w-]+:(#[0-9A-Fa-f]{3,8});", tokens)}
        written = {c.upper() for c in re.findall(r"(?<=[:\s,(])#[0-9A-Fa-f]{3,8}\b", rules)}
        self.assertLessEqual({value.upper() for value in self.TOKENS.values()}, held)   # the block was read
        self.assertEqual(written & held, set())

    def test_reduced_motion_stops_every_animation_and_transition(self):
        from subsync.ui import APP_CSS
        self.assertRegex(APP_CSS, r"prefers-reduced-motion:reduce\)\{\s*\*,\*::before,\*::after\{transition:none!important;"
                                  r"animation:none!important")

    def test_the_breakpoints_of_the_16_shapes(self):
        from subsync.ui import APP_CSS
        self.assertIn("@media (max-width:1023px),(max-height:599px)", APP_CSS)
        self.assertIn("@media (min-width:600px) and (max-width:1023px),(min-width:600px) and (max-height:599px)", APP_CSS)
        self.assertIn("@media (max-height:500px)", APP_CSS)
        self.assertIn(".wrap{max-width:1440px", APP_CSS)

    def test_two_typefaces_from_this_service_only(self):
        from subsync.ui import APP_CSS, FONT_FILES
        self.assertEqual(sorted(re.findall(r"url\(([^)]*)\)", APP_CSS)), sorted(f"/static/fonts/{f}" for f in FONT_FILES))
        families = set(re.findall(r'font-family:"([^"]+)"', APP_CSS))
        self.assertEqual(families, {"Atkinson Hyperlegible Next", "Atkinson Hyperlegible Mono"})
        for banned in ("Inter", "Geist", "Space Grotesk", "DM Sans", "Manrope", "Outfit", "Plus Jakarta", "Recursive"):
            self.assertNotIn(banned, APP_CSS)

    def test_none_of_the_tells_in_the_stylesheet(self):
        from subsync.ui import APP_CSS
        for tell in ("gradient", "blur(", "backdrop-filter", "text-transform:uppercase", "box-shadow:0 0 2", "999px",
                     "rgba(55,138,221", "#6152DF", "border-radius:50px"):
            with self.subTest(tell=tell):
                self.assertNotIn(tell, APP_CSS)

    def test_no_outside_resources_no_inline_style_one_script_one_stylesheet(self):
        from subsync import ui
        for name, text in every_page().items():
            with self.subTest(page=name):
                self.assertNotRegex(text, r"(src|href)=\"(https?:)?//(?!github\.com/haslanli/AIOSubtitles/issues\"|subs\.test/u/)")
                self.assertNotRegex(text, r"<style|\sstyle=|\son[a-z]+=|javascript:")
                tree = Tree(text)
                self.assertEqual([a["src"] for a in tree.all("script")], [ui.JS_URL])
                self.assertEqual([a["href"] for a in tree.all("link") if a.get("rel") == "stylesheet"], [ui.CSS_URL])

    def test_every_page_has_one_main_heading_landmarks_and_a_language(self):
        for name, text in every_page().items():
            with self.subTest(page=name):
                tree = Tree(text)
                self.assertEqual(len(tree.all("h1")), 1)
                self.assertTrue(tree.all("main"))
                self.assertEqual(tree.all("html")[0].get("lang"), "en")
                self.assertTrue(tree.all("header"))

    def test_every_field_has_a_label_and_its_explanation(self):
        for name, text in every_page().items():
            tree = Tree(text)
            labelled = {a.get("for") for a in tree.all("label") if a.get("for")}
            for tag in ("input", "select", "textarea"):
                for a in tree.all(tag):
                    if a.get("type") in ("hidden", "checkbox", "radio") or "hidden" in a:
                        continue
                    with self.subTest(page=name, field=a.get("id")):
                        self.assertIn(a.get("id"), labelled)
            for a in tree.all("input", type="checkbox"):
                with self.subTest(page=name, choice=a.get("id")):
                    self.assertTrue(a.get("aria-describedby"))
                    self.assertIn(a["aria-describedby"], tree.text_of)

    def test_labels_hold_phrasing_content_and_forms_keep_their_role(self):
        # An HTML label holds phrasing content only (the choice and radio rows' words are spans), and ARIA in
        # HTML gives a form no dialog role (the sign-up's layer is the dialog)
        from subsync import ui
        rows = {"choice": ui.choice("c", "Title", "Help."),
                "radio-row": ui.radio_row("r", "v", "Title", "Help.", tag="(tag)", note="Note.")}
        for name, text in dict(every_page(), **rows).items():
            with self.subTest(page=name):
                self.assertEqual(InLabels(text).flow, [])
                self.assertEqual([a["role"] for a in Tree(text).all("form") if "role" in a], [])

    def test_every_text_is_an_approved_text(self):
        # each piece a whole approved text, not a part of one (any fragment passed, even a single word), a
        # value, or one of the few pieces inline markup cuts an approved text into; a Show/Hide button's spoken name
        # is its words before its box's label, itself an approved text
        from subsync import ui
        approved = set(approved_texts()) | split_pieces()
        approved |= {words + text for words in (ui.SHOW, ui.HIDE) for text in approved}
        for name, text in every_page().items():
            for piece in Visible(text).found:
                with self.subTest(page=name, text=piece[:60]):
                    self.assertTrue(piece in approved or DYNAMIC.match(piece), piece)

    def test_the_pieces_cut_by_inline_markup_are_parts_of_approved_texts(self):
        texts = approved_texts()
        for piece in split_pieces():
            with self.subTest(piece=piece):
                self.assertTrue(any(piece in text and piece != text for text in texts), piece)

    def test_each_choice_is_named_by_its_title_alone(self):
        # A tick box or an answer was named by its whole row, title and explanation, then described by the
        # explanation again, so a screen reader read it twice ("Keep my password…" is about 700 characters)
        from subsync import explain
        for name, text in every_page().items():
            tree = Tree(text)
            for a in tree.all("input", type="checkbox") + tree.all("input", type="radio"):
                with self.subTest(page=name, box=a["id"]):
                    title = tree.text_of[a["aria-labelledby"]]
                    self.assertTrue(title)
                    for described in a.get("aria-describedby", "").split():
                        self.assertNotIn(tree.text_of[described], title)
        tree = Tree(every_page()["signup"])
        box = tree.all("input", id="keep_password")[0]
        self.assertEqual(tree.text_of[box["aria-labelledby"]],
                         "Keep my password, so AIOSubtitles follows my config by itself")
        self.assertEqual(tree.text_of[box["aria-describedby"]], explain.field_help("keep_password", settings()))

    def test_on_phones_a_long_label_wraps_and_a_number_box_shrinks(self):
        # At 320 px wide "Stop keeping my password" couldn't wrap, so every settings row ran 15 px into the
        # side margin; setup's number boxes kept their typed width and did the same on its Limits pages (10-18 px)
        self.assertIn("white-space:nowrap", css_rule(".btn"))          # wider, a label stays on one line
        phone = css_media("(max-width:1023px),(max-height:599px)")
        self.assertIn("\n  .btn{white-space:normal;text-align:center}\n", phone)
        self.assertIn("\n  .num input{flex:1;width:0}\n", phone)

    def test_a_focused_box_keeps_an_outline_high_contrast_mode_paints(self):
        # These boxes dropped their outline and showed focus by a border colour and a shadow, which
        # high-contrast mode replaces and removes: setup's number boxes looked the same focused or not
        for selector in (".input:focus", ".sel select:focus", ".num:focus-within"):
            with self.subTest(selector=selector):
                self.assertTrue(css_rule(selector).startswith("outline:2px solid transparent;"), css_rule(selector))
                self.assertIn("border-color:var(--accent-link);", css_rule(selector))   # the look without it the same

    def test_in_high_contrast_mode_the_picked_value_takes_the_highlight(self):
        # The picked segment of "Audio per file" kept only a background and a border colour, both replaced
        # in high-contrast mode, so 0.5, 1, 2 and 4 GB looked alike (the step list's current step stays bold)
        forced = css_media("(forced-colors:active)")
        self.assertIn("\n  .seg input:checked+span{background:Highlight;border-color:Highlight;color:HighlightText}\n",
                      forced)

    def test_without_the_script_the_hidden_continue_bars_take_no_space(self):
        # Each Continue was only made invisible, its bar keeping 86-112 px between the questions; the rule's
        # [data-add] part never applied (that button is hidden until the script shows it)
        from subsync.ui import APP_CSS
        self.assertIn("\nhtml:not(.js) .screen:not(:last-child)>.actions{display:none}\n"
                      "html:not(.js) .steps{visibility:hidden}\n", APP_CSS)
        self.assertNotRegex(APP_CSS, r"html:not\(\.js\) \[data-")
        # those bars hold Continue alone; the last screen's, Sign up and the note under it, stays
        for name in ("signup", "signup-no-invite", "signup-audio"):
            with self.subTest(page=name):
                screens = re.findall(r'<section class="screen".*?</section>', every_page()[name], re.S)
                for screen in screens[:-1]:
                    self.assertRegex(screen, r'<div class="actions"><button class="btn primary" type="button" '
                                             r'data-next>Continue<svg[^>]*><path[^>]*/></svg></button></div>'
                                             r'</section>$')
                self.assertIn("Sign up", re.search(r'<div class="actions">.*', screens[-1], re.S).group(0))

    def test_personal_pages_ask_not_to_be_indexed(self):
        pages = every_page()
        for name in ("result", "settings", "delete", "deleted", "error-personal"):
            self.assertIn('<meta name="robots" content="noindex">', pages[name])
        for name in ("signup", "privacy", "not-found", "error-unexpected"):
            self.assertNotIn("noindex", pages[name])

    def test_the_step_bar_has_a_segment_per_step(self):
        # Four fixed columns; without an invite code the three steps left the last quarter empty
        from subsync import ui
        steps = re.search(r"(?m)^\.steps\{([^}]*)\}", ui.APP_CSS).group(1)
        self.assertIn("display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);", steps)
        self.assertNotIn("grid-template-columns", steps)
        pages = every_page()
        for name, count in (("signup", 4), ("signup-no-invite", 3)):
            with self.subTest(page=name):
                bar = re.search(r'<ol class="steps" id="steps">(.*?)</ol>', pages[name]).group(1)
                self.assertEqual(bar.count("<li>"), count)

    def test_every_value_is_escaped(self):
        from subsync import pages
        s = settings()
        for text in (pages.signup_page(s, error=HOSTILE), pages.error_page(HOSTILE),
                     pages.settings_page(s, dict(RECORD, state="cannot_rebuild", state_detail=HOSTILE), USAGE,
                                         LINK + HOSTILE, now=0, error=HOSTILE, notice=HOSTILE, message=HOSTILE),
                     pages.result_page(result(changes=[HOSTILE], manifest_url=LINK + HOSTILE,
                                              warning=(HOSTILE, HOSTILE))),
                     pages.delete_confirm_page(error=HOSTILE)):
            self.assertNotIn("<b onclick", text)
            self.assertIn("&lt;b onclick=&quot;x&quot;&gt;&amp;&#x27;&quot;", text)


class SignupPageTest(unittest.TestCase):
    def page(self, **kw):
        from subsync import pages
        return pages.signup_page(settings(SUBSYNC_PUBLIC_INVITE="code"), **kw)

    def test_the_landing_and_the_layer(self):
        tree = Tree(self.page())
        self.assertIn("Subtitles, in sync with the file you play.", tree.text())
        opener = [a for a in tree.all("a") if a.get("href") == "#signup"]
        self.assertEqual(len(opener), 2)                    # the page's Sign up and the phone's bar, plain links
        layer = tree.all("div", id="signup")[0]
        self.assertEqual(layer["class"], "layer")
        # the layer is the dialog; the form inside keeps its own role (ARIA in HTML allows no dialog on a form)
        self.assertEqual((layer.get("role"), layer.get("aria-modal"), layer.get("aria-label")),
                         ("dialog", "true", "Sign up"))
        form = tree.all("form")[0]
        self.assertEqual((form["method"], form["action"]), ("post", "/signup"))
        self.assertNotIn("role", form)
        self.assertEqual([a["data-screen"] for a in tree.all("section") if "data-screen" in a],
                         ["invite", "link", "password", "languages", "config"])
        self.assertEqual([a.get("hidden") for a in tree.all("section") if "data-screen" in a], [None] * 5)

    def test_the_steps_named_in_the_approved_words(self):
        text = Tree(self.page()).text()
        for word in ("Invite", "AIOStreams", "Languages", "Config", "Continue", "Before you start"):
            self.assertIn(word, text)

    def test_without_an_invite_code_the_first_step_is_the_link(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings()))
        self.assertEqual([a["data-screen"] for a in tree.all("section") if "data-screen" in a],
                         ["link", "password", "languages", "config"])
        self.assertNotIn("Invite", [li for li in tree.text_of.get("steps", "").split()])

    def test_the_fields_send_what_the_sign_up_reads(self):
        tree = Tree(self.page())
        names = {a.get("name") for t in ("input", "select") for a in tree.all(t)}
        self.assertTrue({"invite", "link", "password", "keep_password", "lang", "lang_ui", "add_addon",
                         "clean_up"} <= names)
        self.assertIn("checked", tree.all("input", id="add_addon")[0])
        self.assertNotIn("checked", tree.all("input", id="clean_up")[0])
        self.assertNotIn("checked", tree.all("input", id="keep_password")[0])
        self.assertEqual(tree.all("input", id="link")[0]["class"], "input mono")

    def test_the_invite_box_asks_phones_for_no_capitals(self):
        # It asked for capitals (autocapitalize="characters"), and a suggested code typed on a phone was refused
        box = Tree(self.page()).all("input", id="invite")[0]
        self.assertEqual((box["autocapitalize"], box["autocomplete"], box["spellcheck"]), ("none", "off", "false"))

    def test_the_clean_up_names_what_this_instance_takes_out_and_is_gone_when_nothing_is(self):
        # The box removes only the addons whose site the instance asks itself, and says which
        from subsync import explain, pages
        lead = "Takes out of your AIOStreams config the subtitle addons AIOSubtitles now searches itself: "
        for env, names in (({}, "OpenSubtitles and OpenSubtitles V3+"),
                           ({"SUBSYNC_SUBDL_KEY": "k", "SUBSYNC_SUBSOURCE_KEY": "k"},
                            "OpenSubtitles, OpenSubtitles V3+, SubDL and SubSource"),
                           ({"SUBSYNC_EXTRA_SOURCES": "osv3plus,tsdb", "SUBSYNC_SUBDL_KEY": "k"},
                            "OpenSubtitles V3+ and SubDL"),
                           ({"SUBSYNC_EXTRA_SOURCES": "osv3", "SUBSYNC_SUBDL_KEY": ""}, "OpenSubtitles")):
            with self.subTest(env=env):
                tree = Tree(pages.signup_page(settings(**env)))
                self.assertTrue(tree.all("input", id="clean_up"))
                self.assertIn(lead + names + ". You then get their subtitles once, through AIOSubtitles", tree.text())
        self.assertTrue(explain.clean_up_help(("opensubtitles", "opensubtitles-v3-plus", "subdl", "subsource"))
                        .startswith(lead + "OpenSubtitles, OpenSubtitles V3+, SubDL and SubSource. "))
        tree = Tree(pages.signup_page(settings(SUBSYNC_EXTRA_SOURCES="tsdb,gestdown")))
        self.assertEqual(tree.all("input", id="clean_up"), [])
        self.assertNotIn(lead, tree.text())
        self.assertTrue(tree.all("input", id="add_addon"))

    def test_the_language_slots_follow_the_instance_limit(self):
        from subsync import explain, pages
        for limit in (1, 2, 3, 4):
            with self.subTest(limit=limit):
                text = pages.signup_page(settings(SUBSYNC_MAX_LANGUAGES=str(limit)))
                tree = Tree(text)
                slots = tree.all("select", name="lang")
                self.assertEqual(len(slots), limit)
                self.assertEqual(tree.all("div", id="languages")[0]["data-max-languages"], str(limit))
                self.assertIn(explain.languages_help(settings(SUBSYNC_MAX_LANGUAGES=str(limit))), tree.text())
                labels = [a["for"] for a in tree.all("label") if a.get("for", "").startswith("lang-")]
                self.assertEqual(labels, [f"lang-{i}" for i in range(1, limit + 1)])

    def test_the_languages_help_states_the_instances_synced_choices(self):
        # One synced choice with SUBSYNC_MAIN_ALTERNATIVES=1 or best fit off, on both pages that ask
        from subsync import pages
        for env in ({"SUBSYNC_MAIN_ALTERNATIVES": "1"}, {"SUBSYNC_BEST_FIT": "off"}):
            with self.subTest(env=env):
                s = settings(**env)
                for page in (pages.signup_page(s), pages.settings_page(s, RECORD, USAGE, LINK, now=0)):
                    text = Tree(page).text()
                    self.assertIn("so fewer is faster. Each gets one synced choice. You can change them later", text)
                    self.assertNotIn("up to three synced choices", text)

    def test_english_first_by_default_and_the_others_none(self):
        text = self.page()
        first = re.search(r'<select id="lang-1"[^>]*>(.*?)</select>', text, re.S).group(1)
        self.assertIn('<option value="eng" selected>English</option>', first)
        self.assertNotIn(">None<", first)
        second = re.search(r'<select id="lang-2"[^>]*>(.*?)</select>', text, re.S).group(1)
        self.assertIn('<option value="" selected>None</option>', second)

    def test_after_a_problem_the_layer_opens_with_the_summary_and_the_field(self):
        text = self.page(error="Wrong.", field="password", order=["spa", "eng"])
        tree = Tree(text)
        self.assertIn("open", tree.all("body")[0]["class"])
        self.assertIn("Sign-up didn't go through", tree.text_of["problem"])
        self.assertEqual([a.get("href") for a in tree.all("a") if a.get("href") == "#password"], ["#password"])
        box = tree.all("input", id="password")[0]
        self.assertEqual(box.get("aria-invalid"), "true")
        self.assertIn("problem-message", box["aria-describedby"].split())     # the summary right above says it once
        self.assertIn("Wrong.", tree.text_of["problem-message"])
        self.assertNotIn("password-error", tree.text_of)
        first = re.search(r'<select id="lang-1"[^>]*>(.*?)</select>', text, re.S).group(1)
        self.assertIn('value="spa" selected', first)
        self.assertNotRegex(text, r'id="(link|password|invite)"[^>]*value=')

    def test_a_problem_without_a_field_is_in_the_summary_only(self):
        tree = Tree(self.page(error="Wrong."))
        self.assertIn("Wrong.", tree.text_of["problem"])
        self.assertFalse([a for a in tree.all("input") if a.get("aria-invalid")])

    def test_after_a_problem_the_layer_opens_at_the_first_emptied_screen(self):
        # It opened at the screen with the problem, past the emptied invite code and link, so going on sent
        # them empty and the retry failed too, using another of the hour's attempts
        from subsync import pages
        for s, first in ((settings(SUBSYNC_PUBLIC_INVITE="code"), "invite"), (settings(), "link"),
                         (settings(SUBSYNC_AUDIO_ROUTE="on"), "link")):
            for field in ("invite", "link", "password", "languages", "audio", None):
                if field == "invite" and first != "invite" or field == "audio" and not s.audio_route:
                    continue
                with self.subTest(first=first, field=field):
                    tree = Tree(pages.signup_page(s, error="Wrong.", field=field))
                    self.assertEqual(tree.all("body")[0]["data-open-at"], first)
                    self.assertIn("open", tree.all("body")[0]["class"])
                    # the summary on top, its link to the field with the problem
                    self.assertEqual([a["href"] for a in tree.all("a") if a.get("href", "").startswith("#")
                                      and a["href"] != "#signup"], [f"#{field}"] if field else [])
        self.assertNotIn("data-open-at", self.page())                             # a fresh page: the layer closed

    def test_the_enter_them_again_note_only_where_trying_again_makes_sense(self):
        # It also asked for them again after too many attempts or on a full instance
        from subsync import explain
        note = explain.PUBLIC_TEXTS["kept_note"]
        self.assertIn(note, Tree(self.page(error="Wrong.", field="password")).text_of["problem"])
        self.assertIn(note, Tree(self.page(error="Wrong.")).text_of["problem"])
        problem = Tree(self.page(error="Wrong.", retry=False)).text_of["problem"]
        self.assertIn("Wrong.", problem)
        self.assertNotIn(note, problem)

    def test_after_a_problem_the_boxes_come_back_as_they_were_left(self):
        # "Add AIOSubtitles…" came back ticked and the other three unticked, whatever the person had chosen
        from subsync import pages
        s = settings(SUBSYNC_USENET="on", SUBSYNC_USENET_LISTINGS_PER_DAY="5")

        def ticked(text):
            tree = Tree(text)
            return {name: "checked" in tree.all("input", type="checkbox", id=name)[0] for name in BOXES}
        self.assertEqual(pages.SIGNUP_BOXES, {"add_addon": True, "clean_up": False, "usenet": False,
                                              "keep_password": False})
        self.assertEqual(tuple(pages.SIGNUP_BOXES), BOXES)
        self.assertEqual(ticked(pages.signup_page(s)), pages.SIGNUP_BOXES)                      # a fresh page
        self.assertEqual(ticked(pages.signup_page(s, error="Wrong.")), pages.SIGNUP_BOXES)      # nothing sent back
        for left in ({name: not on for name, on in pages.SIGNUP_BOXES.items()}, dict.fromkeys(BOXES, True),
                     dict.fromkeys(BOXES, False)):
            with self.subTest(left=left):
                self.assertEqual(ticked(pages.signup_page(s, error="Wrong.", field="password", ticked=left)), left)

    def test_the_privacy_link_above_sign_up_opens_in_a_new_tab(self):
        # It opened in the same tab, and coming back the sign-up was closed and empty
        tree = Tree(self.page())
        links = [a for a in tree.all("a") if a.get("href") == "/privacy"]
        header, above = links
        self.assertEqual(header, {"href": "/privacy"})                            # the header's link is unchanged
        self.assertEqual(above, {"href": "/privacy", "target": "_blank", "rel": "noopener",
                                 "aria-label": "Privacy (opens in a new tab)"})
        last = re.search(r'<section class="screen" data-screen="config".*?</section>', self.page(), re.S).group(0)
        self.assertIn('<a href="/privacy" target="_blank" rel="noopener" aria-label="Privacy (opens in a new tab)">'
                      'Privacy</a>.</p>', last)

    def test_before_you_start_names_the_attempt_limit_from_the_setting(self):
        from subsync import pages
        self.assertIn("Each try counts: 9 sign-up attempts per address in an hour.",
                      Tree(pages.signup_page(settings(SUBSYNC_SIGNUPS_PER_HOUR="9"))).text())

    def test_buttons_the_script_adds_stay_hidden_without_it(self):
        tree = Tree(self.page())
        for a in tree.all("button"):
            if "data-reveal" in a or "data-add" in a or "data-remove-slot" in a:
                self.assertIn("hidden", a)


class ResultPageTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_stand_in_has_the_real_results_fields(self):
        from subsync.signup import SignupResult      # a broken import fails the test (it skipped)
        import dataclasses
        self.assertEqual({f.name for f in dataclasses.fields(SignupResult)}, set(vars(result())))

    def test_the_link_first_then_what_was_done_and_what_next(self):
        from subsync import explain, pages
        tree = Tree(pages.result_page(result()))
        self.assertEqual(tree.text_of["personal"], LINK)
        text = tree.text()
        self.assertLess(text.index("Your personal addon link"), text.index("Done on your AIOStreams"))
        self.assertIn(explain.PUBLIC_TEXTS["made_config"], text)
        self.assertIn("Languages: English, Spanish.", text)
        self.assertIn(f"{BASE}/u/{TOKEN}/configure", [a.get("href") for a in tree.all("a")])

    def test_the_config_changes_are_listed_under_one_item(self):
        # Each change line came after "Changed in your AIOStreams config: ", two colons in one line ("…config:
        # adds 1 subtitle source: …"); the item is said once, each change under it as its own sentence
        from subsync import pages
        from subsync.ui import e
        page = pages.result_page(result(changes=change_lines()))
        self.assertIn("<li>Changed in your AIOStreams config:<ul class=\"dots\">"
                      + "".join(f"<li>{e(line)}</li>" for line in CHANGES) + "</ul></li>", page)
        self.assertNotIn("Changed in your AIOStreams config: ", page)
        already = Tree(pages.result_page(result(changes=[]))).text()
        self.assertIn("AIOSubtitles is in your AIOStreams config already.", already)
        self.assertNotIn("Changed in your AIOStreams config", already)

    def test_manual_steps_when_the_addon_could_not_be_added(self):
        from subsync import explain, pages
        text = Tree(pages.result_page(result(added=False, manual=True, changes=[]))).text()
        self.assertIn("One step left", text)
        self.assertIn(explain.PUBLIC_TEXTS["not_added"], text)
        for step in explain.PUBLIC_MANUAL_STEPS:
            self.assertIn(step, text)

    def test_the_real_debrid_warning(self):
        from subsync import explain, pages
        self.assertIn("Real-Debrid is left out",
                      Tree(pages.result_page(result(warning=explain.PUBLIC_REAL_DEBRID_WARNING))).text())

    def test_stored_copies_only(self):
        from subsync import explain, pages
        text = Tree(pages.result_page(result(store_only=True))).text()
        self.assertIn(explain.PUBLIC_STORE_ONLY[0], text)
        self.assertIn(re.sub(r"\s+", " ", explain.PUBLIC_STORE_ONLY[1]), text)
        self.assertNotIn(explain.PUBLIC_TEXTS["made_config"], text)


class SettingsPageTest(unittest.TestCase):
    def page(self, record=None, usage=None, s=None, **kw):
        from subsync import pages
        return pages.settings_page(s or settings(), record or RECORD, usage or USAGE, LINK, now=kw.pop("now", 0), **kw)

    def test_usage_and_the_reset_time(self):
        text = Tree(self.page(now=86400 * 5 + 3600)).text()
        self.assertIn("3 of 200", text)
        self.assertIn("0.3 MB of 10,000 MB", text)
        self.assertIn("Resets at 00:00 UTC, in 23 h.", text)

    def test_the_figures_have_thousands_separators_as_setup_writes_them(self):
        # "0.3 MB of 10000 MB" here, "10,000" on setup's limits page and in the explanations
        from subsync import pages
        s, record, usage = THOUSANDS
        page = pages.settings_page(s, record, usage, LINK, now=0, usenet_usage={"listings": 1200, "stopped": False})
        values = re.findall(r'<div class="gauge[^"]*"><p><b>([^<]*)</b>', page)
        self.assertEqual(values, ["1,200 of 2,000", "9,500 MB of 10,000 MB", "1,200 of 1,500"])
        self.assertEqual(pages._megabytes(9500 * 1024 * 1024), "9,500")
        self.assertEqual(pages._megabytes(5 * 1024 * 1024), "5.0")              # under 10 MB: one decimal, as before

    def test_the_delete_button_lines_up_with_its_heading(self):
        # The rule that lines a row's buttons up with its heading never matched a settings form, which starts
        # with its hidden action: "Delete my data…" sat 28 px lower
        from subsync import ui
        self.assertIn("\n.row .controls>.actions:first-child,.row .controls>input[type=hidden]:first-child+.actions"
                      "{margin-top:0}\n", ui.APP_CSS)
        row = re.search(r'<section class="grid12 row" aria-labelledby="delete-h">.*?</section>', self.page(), re.S)
        self.assertRegex(row.group(0), r'</div><form class="controls" method="post" action="configure"><input '
                                       r'type="hidden" name="action" value="delete-confirm"><div class="actions">')

    def test_the_reset_time_reads_plainly(self):
        from subsync.pages import reset_in
        self.assertEqual(reset_in(0), "in 24 h")
        self.assertEqual(reset_in(86400 - 60), "in 1 min")
        self.assertEqual(reset_in(86400 - 3600 * 7 - 60 * 12), "in 7 h 12 min")

    def test_a_reached_limit_is_called_out_and_its_bar_turns_red(self):
        text = self.page(usage={"files": 200, "bytes": 0})
        self.assertIn("Today's limit is reached", Tree(text).text())
        self.assertIn('class="gauge full"', text)

    def test_success_warning_and_problem_each_look_different(self):
        text = self.page(message="Saved.", notice="Careful.", error="Wrong.", field="refresh")
        self.assertIn('class="note ok"', text)
        self.assertIn('class="note warn"', text)
        self.assertIn('class="note problem"', text)

    def test_languages_in_their_order_with_the_limit(self):
        from subsync import pages
        text = self.page(record={"languages": ["spa", "eng"]})
        self.assertIn('value="spa" selected', re.search(r'<select id="lang-1".*?</select>', text, re.S).group(0))
        self.assertIn('value="eng" selected', re.search(r'<select id="lang-2".*?</select>', text, re.S).group(0))
        tree = Tree(pages.settings_page(settings(SUBSYNC_MAX_LANGUAGES="2"), {"languages": ["eng", "spa", "fre"]}, USAGE,
                                        LINK, now=0))
        self.assertEqual(len(tree.all("select", name="lang")), 3)     # a lowered limit keeps them until saved

    def test_refresh_error_is_at_the_password(self):
        tree = Tree(self.page(error="Wrong.", field="refresh"))
        self.assertEqual(tree.all("input", id="refresh")[0].get("aria-invalid"), "true")
        self.assertEqual(tree.all("input", id="refresh")[0].get("name"), "password")

    def test_the_kept_password_shows_stop_keeping(self):
        def forget(text):
            return Tree(text).all("input", name="action", value="forget-password")
        self.assertTrue(forget(self.page(record=dict(RECORD, auto_password="x"))))
        self.assertFalse(forget(self.page()))

    def test_stored_copies_only_says_so_and_has_no_usage(self):
        from subsync import explain
        text = Tree(self.page(record=dict(RECORD, store_only=True))).text()
        self.assertIn(explain.PUBLIC_STORE_ONLY[0], text)
        self.assertNotIn("Today", text)
        self.assertNotIn("was copied from yours", text)
        self.assertIn("Refresh", text)

    def test_done_holds_every_code_a_save_sends(self):
        # DONE is done_message's own table, so every_page renders the settings page after each code public.py
        # sends back (configure?done=<code>), the TMDB key's two included
        from subsync import pages
        with open(os.path.join(os.path.dirname(os.path.abspath(pages.__file__)), "public.py"), encoding="utf-8") as f:
            sent = set(re.findall(r'_see\("([a-z-]+)"\)', f.read()))               # the codes sent as they are
        self.assertLessEqual({"languages", "audio", "forgot", "tmdb-key", "tmdb-key-removed"}, sent)
        sent |= {"refreshed", "refreshed-rd", "refreshed-manual"}                    # a refresh's, by its result
        sent |= {kind + manual for kind in ("to-stored", "stored") for manual in ("", "-manual")}   # stored copies only
        self.assertEqual(set(pages.DONE), sent)

    def test_the_refresh_form_sends_only_what_the_server_reads(self):
        # The server adds AIOSubtitles on every refresh by itself, so the form sends no add_addon of its own
        text = self.page(s=settings(SUBSYNC_USENET="on", SUBSYNC_USENET_LISTINGS_PER_DAY="5"))
        form = re.search(r'<form method="post" action="configure"><input type="hidden" name="action" value="refresh">'
                         r'(.*?)</form>', text, re.S).group(1)
        self.assertEqual(set(re.findall(r'name="([^"]+)"', form)), {"password", "link", "keep_password", "usenet"})

    def test_done_messages_are_fixed_texts(self):
        from subsync import explain, pages
        record = {"languages": ["eng", "spa"]}
        self.assertEqual(pages.done_message("languages", record),
                         ("Languages saved: English, Spanish. They apply from the next file you play.", None))
        self.assertEqual(pages.done_message("refreshed", record), (explain.PUBLIC_TEXTS["refreshed"], None))
        self.assertEqual(pages.done_message("refreshed-rd", record),
                         (explain.PUBLIC_TEXTS["refreshed"], explain.PUBLIC_REAL_DEBRID_NOTICE))
        manual = " ".join((explain.PUBLIC_TEXTS["not_added"],) + explain.PUBLIC_MANUAL_STEPS)
        self.assertEqual(pages.done_message("to-stored", record), (explain.PUBLIC_TEXTS["to_stored"], None))
        self.assertEqual(pages.done_message("to-stored-manual", record), (explain.PUBLIC_TEXTS["to_stored"], manual))
        self.assertEqual(pages.done_message("stored", record), (None, None))
        self.assertEqual(pages.done_message("stored-manual", record), (None, manual))
        self.assertEqual(pages.done_message("forgot", record), (explain.PUBLIC_TEXTS["forgot"], None))
        self.assertEqual(pages.done_message("<script>", record), (None, None))


class AudioChoiceTest(unittest.TestCase):
    """Each person's answer for files with no subtitles of their own, on the sign-up's config screen and the
    settings page: shown only with the instance's audio route on, two answers while it allows no opened syncs, the
    person's own answer picked, and the note under "as soon as a file is opened" shown without the script."""

    def on(self, **env):
        return settings(SUBSYNC_AUDIO_ROUTE="on", **env)

    def settings_page(self, s, record=None, **kw):
        from subsync import pages
        return pages.settings_page(s, record or RECORD, USAGE, LINK, now=0, **kw)

    @staticmethod
    def radios(text):
        return [(a["value"], "checked" in a) for a in Tree(text).all("input", type="radio", name="audio")]

    @staticmethod
    def config_screen(text):
        return re.search(r'<section class="screen" data-screen="config".*?</section>', text, re.S).group(0)

    def test_the_sign_up_asks_it_on_the_config_screen(self):
        from subsync import explain, pages
        s = self.on()
        screen = self.config_screen(pages.signup_page(s))
        self.assertEqual(self.radios(screen), [("off", False), ("pick", True), ("open", False)])
        tree = Tree(screen)
        group = tree.all("fieldset", id="audio")[0]
        self.assertEqual(group["aria-describedby"], "audio-help")
        self.assertEqual(tree.text_of["audio-help"], explain.audio_help(s))
        self.assertIn("Files with no subtitles of their own", tree.text())
        self.assertEqual(tree.text_of["audio-open-help"], explain.audio_answer_help("open", s))
        self.assertIn("(at most 5 a day)", tree.text_of["audio-open-help"])
        self.assertIn("at most 1 GB", tree.text_of["audio-help"])
        self.assertIn("at most 20 a day", tree.text_of["audio-help"])
        for off in (settings(), settings(SUBSYNC_AUDIO_ROUTE="off")):
            text = pages.signup_page(off)
            self.assertEqual(self.radios(text), [])
            self.assertNotIn("Files with no subtitles of their own", text)

    def test_two_answers_while_the_instance_allows_no_opened_syncs(self):
        from subsync import explain, pages
        none = self.on(SUBSYNC_USER_OPENED_PER_DAY="0")
        self.assertEqual(self.radios(pages.signup_page(none)), [("off", False), ("pick", True)])
        page = self.settings_page(none, dict(RECORD, audio="open"))
        self.assertEqual(self.radios(page), [("off", False), ("pick", True)])     # their "open" reads "pick"
        self.assertNotIn(explain.PUBLIC_AUDIO["opened_note"], Tree(page).text())
        self.assertNotIn("as soon as a file is opened", Tree(page).text())

    def test_the_settings_page_shows_the_persons_answer_and_saves_it_with_the_audio_action(self):
        for audio, picked in ((None, "pick"), ("off", "off"), ("pick", "pick"), ("open", "open"), ("loud", "pick")):
            with self.subTest(audio=audio):
                record = dict(RECORD, **({"audio": audio} if audio else {}))
                self.assertEqual([v for v, chosen in self.radios(self.settings_page(self.on(), record)) if chosen],
                                 [picked])
        tree = Tree(self.settings_page(self.on()))
        self.assertEqual(tree.text_of["audio-h"], "Files with no subtitles of their own")
        group = tree.all("fieldset", id="audio")[0]
        self.assertEqual((group["aria-labelledby"], group["aria-describedby"]), ("audio-h", "audio-help"))
        self.assertTrue(tree.all("input", type="hidden", name="action", value="audio"))
        self.assertIn("Save answer", tree.text())
        for off in (settings(), settings(SUBSYNC_AUDIO_ROUTE="off")):
            text = self.settings_page(off, dict(RECORD, audio="open"))
            self.assertEqual(self.radios(text), [])
            self.assertNotIn("audio-h", text)

    def test_the_note_under_the_third_answer_needs_no_script(self):
        from subsync import explain, pages, ui
        text = self.settings_page(self.on())
        tree = Tree(text)
        self.assertEqual(tree.text_of["audio-open-note"], explain.PUBLIC_AUDIO["opened_note"])
        self.assertEqual(tree.all("span", id="audio-open-note")[0]["class"], "if-chosen")
        self.assertEqual(tree.all("input", id="audio-open")[0]["aria-describedby"], "audio-open-help audio-open-note")
        self.assertNotIn("audio-off-note", tree.text_of)
        # the stylesheet hides it while another answer is picked; a browser that can't tell shows it
        self.assertIn(".way:not(:has(input:checked)) .if-chosen{display:none}", ui.APP_CSS)
        # a plain form: choosing and saving work without the script
        form = re.search(r'<form[^>]*>(?:(?!</form>).)*name="audio"(?:(?!</form>).)*</form>', text, re.S).group(0)
        self.assertRegex(form, r'^<form class="controls" method="post" action="configure">')
        self.assertIn('type="submit"', form)
        self.assertNotIn("data-", form)
        signup = Tree(self.config_screen(pages.signup_page(self.on())))
        self.assertEqual(signup.text_of["audio-open-note"], explain.PUBLIC_AUDIO["opened_note"])

    def test_after_a_problem_the_answer_is_kept_and_the_summary_links_to_its_screen(self):
        from subsync import pages
        text = pages.signup_page(self.on(), error="Wrong.", field="audio", audio="open")
        self.assertEqual([v for v, chosen in self.radios(text) if chosen], ["open"])
        tree = Tree(text)
        # The layer opens at the first emptied screen (the link: no invite code here), not the config screen
        self.assertEqual(tree.all("body")[0]["data-open-at"], "link")
        self.assertEqual([a["href"] for a in tree.all("a") if a.get("href") == "#audio"], ["#audio"])
        for audio in ("loud", None, "open"):
            with self.subTest(audio=audio):
                s = self.on(SUBSYNC_USER_OPENED_PER_DAY="0") if audio == "open" else self.on()
                text = pages.signup_page(s, error="Wrong.", field="link", audio=audio)
                self.assertEqual([v for v, chosen in self.radios(text) if chosen], ["pick"])
        tree = Tree(self.settings_page(self.on(), error="Wrong.", field="audio"))
        self.assertEqual([a["href"] for a in tree.all("a") if a.get("href") == "#audio"], ["#audio"])

    def test_the_answers_problem_is_tied_to_them_as_the_other_fields_are(self):
        # found by a screen check: the answers were not marked or described by their problem; now, as for the other
        # fields, on the sign-up by the summary right above, on the settings page by the row's own error, as Languages'
        from subsync import pages
        tree = Tree(self.config_screen(pages.signup_page(self.on(), error="Wrong.", field="audio", audio="open")))
        self.assertEqual(tree.all("fieldset", id="audio")[0]["aria-describedby"].split(), ["problem-message", "audio-help"])
        self.assertEqual([a.get("aria-invalid") for a in tree.all("input", type="radio", name="audio")], ["true"] * 3)
        tree = Tree(self.settings_page(self.on(), error="Wrong.", field="audio"))
        self.assertEqual(tree.text_of["audio-error"], "Wrong.")
        self.assertEqual(tree.all("p", id="audio-error")[0]["class"], "field-error")
        self.assertEqual(tree.all("fieldset", id="audio")[0]["aria-describedby"].split(), ["audio-error", "audio-help"])
        self.assertEqual([a.get("aria-invalid") for a in tree.all("input", type="radio", name="audio")], ["true"] * 3)
        self.assertIn("Wrong.", tree.text_of["problem-message"])           # the summary at the top stays
        # no problem, or one about another field: nothing marked
        for kw in ({}, {"error": "Wrong.", "field": "languages"}):
            for text in (self.settings_page(self.on(), **kw), pages.signup_page(self.on(), **kw)):
                tree = Tree(text)
                self.assertNotIn("audio-error", tree.text_of)
                self.assertEqual(tree.all("fieldset", id="audio")[0]["aria-describedby"], "audio-help")
                self.assertFalse([a for a in tree.all("input", type="radio", name="audio") if a.get("aria-invalid")])

    def test_the_note_under_the_third_answer_doesnt_repeat_its_tag(self):
        # found by a screen check: the answer ends "(not recommended)", so its note doesn't start with it again
        from subsync import explain
        note = explain.PUBLIC_AUDIO["opened_note"]
        self.assertTrue(note.startswith("This reads about 60–350 MB of each opened file that has no subtitle track"))
        self.assertNotIn("recommended", note.lower())

    def test_the_saved_message_names_the_answer(self):
        from subsync import explain, pages
        for audio, answer in (("off", "Off"), ("pick", "On, when I pick the file"),
                              ("open", "On, as soon as a file is opened"), (None, "On, when I pick the file")):
            with self.subTest(audio=audio):
                self.assertEqual(pages.done_message("audio", dict(RECORD, audio=audio)),
                                 (explain.PUBLIC_TEXTS["audio_saved"].format(answer=answer), None))
        self.assertIn("audio", pages.DONE)


class OtherPagesTest(unittest.TestCase):
    def test_delete_confirm_lists_what_goes_and_shows_an_error_at_the_password(self):
        from subsync import explain, pages
        tree = Tree(pages.delete_confirm_page(error="Wrong.", field="delete"))
        for line in explain.PUBLIC_DELETE_LIST.values():
            self.assertIn(line, tree.text())
        self.assertEqual(tree.all("input", id="delete")[0].get("aria-invalid"), "true")
        self.assertIn("Nothing was deleted", tree.text())
        self.assertEqual(tree.all("input", type="hidden", name="action")[0]["value"], "delete")
        stored = Tree(pages.delete_confirm_page(stored=True)).text()
        self.assertNotIn(explain.PUBLIC_DELETE_LIST["config"], stored)       # no config of its own to remove

    def test_for_stored_copies_only_delete_doesnt_say_it_removes_a_config(self):
        # The delete step's list left the config out, but the explanation under it, and the settings page's
        # Delete row, still said Delete "removes its own config from your AIOStreams"
        from subsync import explain, pages
        stored = ("Deletes everything AIOSubtitles keeps about you; your personal link stops working at once. "
                  "Optional: with your AIOStreams password it also takes itself out of your AIOStreams config; without "
                  "it, remove the AIOSubtitles addon there yourself. The password is used once and not stored. How "
                  "subtitles were synced stays in the shared store: it belongs to files, not to you.")
        others = ("Deletes everything AIOSubtitles keeps about you and removes its own config from your AIOStreams; "
                  "your personal link stops working at once. Optional: with your AIOStreams password it also takes "
                  "itself out of your AIOStreams config; without it, remove the AIOSubtitles addon there yourself. The "
                  "password is used once and not stored. How subtitles were synced stays in the shared store: it "
                  "belongs to files, not to you.")
        self.assertEqual(explain.PUBLIC_FIELDS["delete_stored"], stored)
        self.assertEqual(explain.PUBLIC_FIELDS["delete"], others)
        s = settings()
        for is_stored, help_text in ((True, stored), (False, others)):
            with self.subTest(stored=is_stored):
                step = Tree(pages.delete_confirm_page(stored=is_stored, settings=s))
                row = Tree(pages.settings_page(s, dict(RECORD, store_only=is_stored), USAGE, LINK, now=0))
                self.assertEqual(step.text_of["delete-help"], help_text)
                self.assertEqual(row.text_of["delete-help"], help_text)
                said = step.text() + row.text()
                self.assertEqual("removes its own config" in said, not is_stored)

    def test_an_unexpected_failure_has_its_page(self):
        # The page existed but was never shown; a browser got a blank page
        from subsync import explain, pages
        self.assertEqual(explain.PUBLIC_TEXTS["went_wrong"],
                         "AIOSubtitles ran into a problem on its side. Try again in a few minutes.")
        for personal in (False, True):
            with self.subTest(personal=personal):
                tree = Tree(pages.error_page(settings=settings(SUBSYNC_CONTACT=CONTACT), personal=personal))
                text = tree.text()
                self.assertIn("Something went wrong " + explain.PUBLIC_TEXTS["went_wrong"] + " Back to the start page",
                              text)
                self.assertEqual([a["href"] for a in tree.all("a")], ["/", "/privacy", CONTACT, "/"])
                self.assertEqual(bool(tree.all("meta", name="robots", content="noindex")), personal)
        self.assertIn("Wrong.", Tree(pages.error_page("Wrong.")).text())          # a page given its own line

    def test_deleted_page_says_what_is_left_to_do(self):
        from subsync import explain, pages
        text = Tree(pages.deleted_page({})).text()
        self.assertIn(explain.PUBLIC_TEXTS["sync_left"], text)
        self.assertIn(explain.PUBLIC_TEXTS["addon_left"], text)
        clean = Tree(pages.deleted_page({"sync_removed": True, "addon_removed": True})).text()
        self.assertNotIn("One step left", clean)

    def test_privacy_in_four_parts_with_the_same_words(self):
        from subsync import explain, pages
        tree = Tree(pages.privacy_page(settings()))
        # no Questions part without the instance's contact; one with it, linking to it
        self.assertEqual(len(tree.all("h2")), len(explain.PRIVACY_PARTS))
        hosted = Tree(pages.privacy_page(settings(SUBSYNC_CONTACT=CONTACT)))
        self.assertEqual(len(hosted.all("h2")), len(explain.PRIVACY_PARTS) + 1)
        self.assertIn("Questions " + CONTACT, hosted.text())
        sites = explain.PRIVACY_SITES.format(sites="OpenSubtitles, TheSubtitleDB, Gestdown")
        default = {"audio": "", "audio_answer": "", "tmdb": "",           # what settings() fills in
                   "asked": f" {sites} {explain.PRIVACY_CINEMETA} {explain.PRIVACY_LOOKUP['wikidata']}",
                   "backups": explain.BACKUP_WORDS["stored"][1], "front": explain.PRIVACY_FRONT[1],
                   "backups_kept": explain.BACKUP_WORDS["kept"][1], "keep": "180 days"}
        for head, text in explain.PRIVACY_PARTS:
            text = text.format(**default)
            self.assertIn(head, tree.text())
            self.assertIn(re.sub(r"\s+", " ", text[:1].upper() + text[1:]), tree.text())

    def test_not_found_page(self):
        from subsync import explain, pages
        text = Tree(pages.not_found_page()).text()
        self.assertIn("This page doesn't exist", text)
        self.assertIn(explain.PUBLIC_TEXTS["not_found"], text)


class InstanceWordsTest(unittest.TestCase):
    """Cycle 1's vis3-explain-3 on the pages: what they say follows the instance's settings."""

    @staticmethod
    def every_kind(s):
        """Each public page, made as public.py makes it, with the instance's settings."""
        from subsync import pages
        kept = dict(RECORD, auto_password="x")
        return {"start": pages.signup_page(s), "signed up": pages.result_page(result(), s),
                "settings": pages.settings_page(s, kept, USAGE, LINK, now=0), "privacy": pages.privacy_page(s),
                "delete": pages.delete_confirm_page(settings=s), "deleted": pages.deleted_page({}, s),
                "not found": pages.not_found_page(s)}

    def test_no_audio_syncs_a_day_takes_the_audio_question_off_the_pages(self):
        # SUBSYNC_USER_AUDIO_PER_DAY 0 still asked the question; the audio limit 0 read "at most no set limit"
        from subsync import explain, pages
        s = settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_USER_AUDIO_PER_DAY="0")
        for name, page in self.every_kind(s).items():
            with self.subTest(page=name):
                self.assertNotIn('name="audio"', page)
                self.assertNotIn('id="audio-h"', page)
                self.assertNotIn("Files with no subtitles of their own", page)
                for words in explain.PUBLIC_AUDIO.values():
                    self.assertNotIn(ui_text(words[:60]), page)
                self.assertNotIn("your answer for files with no subtitles of their own", page)
        self.assertIn('name="audio"', pages.signup_page(settings(SUBSYNC_AUDIO_ROUTE="on")))
        unlimited = settings(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_AUDIO_BUDGET_GB="0")
        for page in (pages.signup_page(unlimited), pages.privacy_page(unlimited)):
            self.assertNotIn("no set limit", page)
        self.assertIn("about 60–350 MB a file, and works one to three minutes", Tree(pages.signup_page(unlimited)).text())
        self.assertIn("short parts of its sound (about 60–350 MB) through", Tree(pages.privacy_page(unlimited)).text())

    def test_the_kept_password_box_only_while_the_check_runs(self):
        # With SUBSYNC_RECHECK_HOURS 0 nothing uses a kept password; otherwise the pages say how often
        from subsync import pages
        off = settings(SUBSYNC_RECHECK_HOURS="0")
        kept = dict(RECORD, auto_password="x")
        self.assertNotIn('name="keep_password"', pages.signup_page(off))
        page = pages.settings_page(off, kept, USAGE, LINK, now=0)
        tree = Tree(page)
        self.assertNotIn('name="keep_password"', page)
        self.assertTrue(tree.all("input", name="action", value="forget-password"))     # it can still be deleted
        self.assertIn("was copied from yours at sign-up. After you add or remove a service or addon in AIOStreams, "
                      "press Refresh.", tree.text())
        self.assertNotIn("follows changes to your config by itself", tree.text())
        six = settings(SUBSYNC_RECHECK_HOURS="6")
        self.assertIn("encrypted so that it opens only with your personal link, and about every 6 hours while you use "
                      "it rebuilds its own config from yours,", Tree(pages.signup_page(six)).text())
        tree = Tree(pages.settings_page(six, kept, USAGE, LINK, now=0))
        self.assertIn("AIOSubtitles keeps your password and follows changes to your config by itself, about every 6 "
                      "hours while you use it.", tree.text())
        self.assertTrue(tree.all("input", name="keep_password"))
        self.assertIn("about once a day while you use it rebuilds", Tree(pages.signup_page(settings())).text())

    def test_the_hosts_own_contact_proxy_and_backups(self):
        # Every instance named the project's issues, Cloudflare and 30-day backups
        from subsync import explain
        questions = f'<a href="{CONTACT}">Questions</a>'
        for name, page in self.every_kind(settings()).items():
            with self.subTest(page=name, host=False):
                self.assertNotIn(">Questions<", page)
                self.assertNotIn(explain.CONTACT, page)
                self.assertNotIn("Cloudflare", page)
                self.assertNotIn("30 days", page)
        for name, page in self.every_kind(settings(**HOST)).items():
            with self.subTest(page=name, host=True):
                self.assertIn(questions, page)
                self.assertNotIn(explain.CONTACT, page)
        texts = {name: Tree(page).text() for name, page in self.every_kind(settings(**HOST)).items()}
        self.assertIn("AIOSubtitles runs behind Cloudflare, which passes every request on", texts["privacy"])
        self.assertIn("Backups of the stored data are kept for up to 30 days. How long it is kept", texts["privacy"])
        self.assertIn("Questions " + CONTACT, texts["privacy"])
        self.assertIn("to delete it at once; the encrypted backups keep it up to 30 days more.", texts["settings"])
        plain = Tree(self.every_kind(settings())["settings"]).text()
        self.assertIn("to delete it at once; the host's encrypted backups may keep it longer.", plain)
        # TMDB's credit stays where it was: the letters get the instance's settings for their header only
        for name, page in self.every_kind(settings(SUBSYNC_TMDB_KEY="k", **HOST)).items():
            with self.subTest(page=name, tmdb=True):
                self.assertEqual(explain.TMDB_NOTICE in page, name in ("start", "signed up", "settings", "privacy"))

    def test_counts_of_one(self):
        # "1 sign-up attempts", "at most 1 extra searches", "(0 of 1 searches used today)"
        from subsync import pages
        one = settings(SUBSYNC_SIGNUPS_PER_HOUR="1", SUBSYNC_USENET="on", SUBSYNC_USENET_LISTINGS_PER_DAY="1")
        start = Tree(pages.signup_page(one)).text()
        self.assertIn("Each try counts: 1 sign-up attempt per address in an hour.", start)
        self.assertIn("The cost: at most 1 extra search a day through your indexers", start)
        record = dict(RECORD, sync_uuid="s", usenet=True, usenet_uuid="u", usenet_services=["nzbdav"])
        page = pages.settings_page(one, record, USAGE, LINK, now=0, usenet_usage={"listings": 0, "stopped": False})
        self.assertIn("Usenet: on, through NzbDAV (0 of 1 search used today)", Tree(page).text())
        self.assertIn("<p><b>0 of 1</b><span>search used today</span></p>", page)          # the gauge
        self.assertNotIn("searches used today", page)
        self.assertIn("<span>searches used today</span>", pages.settings_page(
            settings(SUBSYNC_USENET="on"), record, USAGE, LINK, now=0, usenet_usage={"listings": 0, "stopped": False}))

    def test_the_privacy_page_says_what_this_instance_asks_and_keeps(self):
        # The sites it asks, Cinemeta with Gestdown only;: its record days;: the audio answer
        from subsync import pages
        owner = Tree(pages.privacy_page(settings(SUBSYNC_SUBDL_KEY="k", SUBSYNC_SUBSOURCE_KEY="k"))).text()
        self.assertIn("AIOSubtitles also searches subtitle sites itself (OpenSubtitles, TheSubtitleDB, SubDL, SubSource, "
                      "Gestdown): they get", owner)
        self.assertIn("never your internet address. For series, it first asks Cinemeta for TheTVDB's number by the "
                      "title's id alone. To time subtitles", owner)
        self.assertNotIn("Cinemeta", Tree(pages.privacy_page(settings(SUBSYNC_EXTRA_SOURCES="osv3,tsdb"))).text())
        self.assertIn("or 30 days after your last request;",
                      Tree(pages.privacy_page(settings(SUBSYNC_USER_KEEP_DAYS="30"))).text())
        self.assertIn("your languages, your answer for files with no subtitles of their own, and when your AIOStreams "
                      "was last checked", Tree(pages.privacy_page(settings(SUBSYNC_AUDIO_ROUTE="on"))).text())


def ui_text(text):
    """A text as the pages write it (escaped)."""
    from subsync.ui import e
    return e(text)


class ScriptTest(unittest.TestCase):
    def test_the_script_is_small_local_and_respects_reduced_motion(self):
        from subsync.ui import APP_JS
        self.assertLess(len(APP_JS.encode()), 16 * 1024)
        self.assertIn("prefers-reduced-motion", APP_JS)
        # its one request: a setup page whose work runs fetches its own address, which the setup page's
        # security header allows (connect-src 'self'), the site's not
        self.assertEqual(APP_JS.count("fetch(location.href)"), 1)
        code = APP_JS.replace("fetch(location.href)", "")
        self.assertNotRegex(code, r"https?://|\beval\(|new Function|innerHTML|outerHTML|insertAdjacentHTML|"
                                  r"document\.write|localStorage|sessionStorage|fetch\(|XMLHttpRequest|sendBeacon|"
                                  r"setAttribute\(\s*['\"]style")
        self.assertNotRegex(APP_JS, r"\bAD\d{1,3}\b|S4-\d+")

    def test_a_submit_before_the_last_screen_is_continue(self):
        # Only Enter in a text box was stopped; Enter on a checkbox or a select (the browser's implicit
        # submission) sent the sign-up from the middle, with the config screen's choices never seen
        from subsync.ui import APP_JS
        guard = re.search(r'form\.addEventListener\("submit", function \(e\) \{(?P<body>.*?)\}\);', APP_JS, re.S)
        self.assertIsNotNone(guard, "the sign-up form has no submit guard")
        self.assertRegex(guard["body"], r"^\s*if \(at === screens\.length - 1\) return;\s*e\.preventDefault\(\);\s*"
                                        r"enter\(at \+ 1\);\s*paint\(false\);\s*$")

    def test_the_phones_back_goes_where_the_arrow_goes(self):
        # The layer added no entry to the page's history, so the phone's Back button or gesture left the site
        # and what was typed with it; each move to a screen is an entry now, and the arrow goes back through them too
        from subsync.ui import APP_JS
        self.assertRegex(APP_JS, r'var enter = function \(to\) \{\s*history\.pushState\(\{ screen: to, '
                                 r'depth: depth\(\) \+ 1 \}, ""\);\s*at = to;\s*\};')
        self.assertEqual(APP_JS.count("history.pushState("), 1)            # enter() is the one way forward
        for move in ("enter(found < 0 ? 0 : found);",                      # opening
                     "enter(Math.min(at + 1, screens.length - 1)); paint(false);",   # Continue
                     "enter(at + 1);",                                     # Enter in a field
                     "enter(screens.indexOf(screen));"):                   # a problem's link
            with self.subTest(move=move):
                self.assertEqual(APP_JS.count(move), 1)
        self.assertNotRegex(APP_JS, r"\bat [-+]= 1")                         # no step around the history
        self.assertIn('else if (t.hasAttribute("data-back") && layer.contains(t)) { e.preventDefault(); '
                      'history.back(); }', APP_JS)
        back = re.search(r'window\.addEventListener\("popstate", function \(e\) \{(?P<body>.*?)\n    \}\);', APP_JS,
                         re.S)
        self.assertIsNotNone(back, "nothing answers the phone's Back")
        self.assertRegex(back["body"], r'^\s*if \(!body\.classList\.contains\("open"\)\) return;\s*'
                                       r'var to = e\.state && e\.state\.screen;\s*'
                                       r'if \(typeof to !== "number"\) \{ close\(\); return; \}\s*'
                                       r'var back = to < at;\s*at = to;\s*paint\(back\);\s*$')
        # Escape closes at once and goes back past the layer's entries; a page loaded again starts closed
        self.assertRegex(APP_JS, r"var added = depth\(\);\s*close\(\);\s*if \(added\) history\.go\(-added\);")
        self.assertRegex(APP_JS, r"var earlier = depth\(\);\s*if \(earlier\) history\.replaceState\(null, \"\"\);")
        self.assertIn("if (earlier) history.go(-earlier);", APP_JS)

    def test_a_show_button_says_what_it_does_next(self):
        # The script switched the visible word only; the spoken name follows it, both from the page's words
        from subsync.ui import APP_JS
        reveal = re.search(r'each\("button\[data-reveal\]", function \(button\) \{.*?\n  \}\);', APP_JS, re.S).group(0)
        self.assertIn('button.textContent = button.getAttribute(showing ? "data-show" : "data-hide");\n      '
                      'button.setAttribute("aria-label", button.getAttribute(showing ? "data-show-label" : '
                      '"data-hide-label"));', reveal)
        self.assertNotIn("aria-pressed", APP_JS)                          # no pressed state: the label changes

    def test_the_script_writes_no_words_of_its_own(self):
        # The script wrote the remove button's "Remove: " itself, a copy of an approved word that could drift
        # from the page's; every word it shows comes from the page (data-show, data-hide, data-done, data-remove-label)
        from subsync import ui
        code = re.sub(r"//[^\n]*", "", ui.APP_JS)
        words = {a or b for a, b in re.findall(r"\"([A-Z][^\"]*)\"|'([A-Z][^']*)'", code)}
        self.assertEqual(words - {"Enter", "Escape"}, set())            # key names, not shown to anyone
        slots = ui.language_slots([("English", "eng"), ("Spanish", "spa")], ["eng", "spa"], 2, "l", "h")
        self.assertIn('data-remove-label="Remove: "', slots)
        self.assertIn('aria-label="Remove: Spanish"', slots)

    def test_the_script_uses_only_approved_words(self):
        from subsync.ui import APP_JS
        corpus = approved_corpus()
        for word in re.findall(r"\"([A-Z][^\"]*)\"|'([A-Z][^']*)'", APP_JS):
            word = word[0] or word[1]
            if word in ("Enter", "Escape"):                # key names, not shown to anyone
                continue
            with self.subTest(word=word):
                self.assertIn(word, corpus)


class PartsTest(unittest.TestCase):
    """The installer's parts, built with the site's pages for the installer's own screens."""

    def test_radio_rows(self):
        from subsync import ui
        tree = Tree(ui.radio_group("How?", [ui.radio_row("way", "self", "A", "a.", checked=True),
                                            ui.radio_row("way", "host", "B", "b.", tag="T")]))
        radios = tree.all("input", type="radio")
        self.assertEqual([(a["name"], a["value"], "checked" in a) for a in radios],
                         [("way", "self", True), ("way", "host", False)])
        self.assertEqual(tree.text_of["way-host-help"], "b.")
        self.assertIn("How?", tree.text())

    def test_a_radio_group_with_its_explanation_and_a_note_for_the_picked_row(self):
        # The public pages' answers for files with no subtitles of their own
        from subsync import ui
        tree = Tree(ui.radio_group("How?", [ui.radio_row("w", "a", "A", ""), ui.radio_row("w", "b", "B", "b.",
                                                                                          note="Careful.")],
                                   help_text="Why.", id="w"))
        group = tree.all("fieldset")[0]
        self.assertEqual((group["id"], group["aria-describedby"]), ("w", "w-help"))
        self.assertEqual(tree.text_of["w-help"], "Why.")
        self.assertNotIn("aria-describedby", tree.all("input", id="w-a")[0])     # no words of its own
        self.assertNotIn("w-a-help", tree.text_of)
        self.assertEqual(tree.all("input", id="w-b")[0]["aria-describedby"], "w-b-help w-b-note")
        self.assertEqual(tree.all("span", id="w-b-note")[0]["class"], "if-chosen")
        named = Tree(ui.radio_group(None, [ui.radio_row("w", "a", "A", "a.")], labelled_by="h", described_by="h-help"))
        self.assertFalse(named.all("legend"))
        group = named.all("fieldset")[0]
        self.assertEqual((group["aria-labelledby"], group["aria-describedby"]), ("h", "h-help"))

    def test_the_step_frame(self):
        from subsync import ui
        tree = Tree(ui.step_frame(["One", "Two", "Three"], 1, "<h1>Two</h1>", "Setup steps"))
        rail = tree.all("ol", **{"class": "rail"})[0]
        self.assertEqual(rail["aria-label"], "Setup steps")
        current = [a for a in tree.all("li") if a.get("aria-current") == "step"]
        self.assertEqual(len(current), 2)                         # once in the rail, once in the phone's segments
        self.assertEqual(len([a for a in tree.all("li") if a.get("class") == "done"]), 2)
        self.assertIn("data-back", tree.all("button", **{"aria-label": "Back"})[0])
        done = Tree(ui.step_frame(["One", "Two"], 0, "", "Setup steps", done=True, back=False))
        self.assertFalse([a for a in done.all("li") if a.get("aria-current")])
        self.assertFalse(done.all("button"))

    def test_the_number_field_asks_for_its_keypad_and_has_no_pattern(self):
        # Every box asked for digits only and had a digits-only pattern, so a decimal couldn't be typed on a
        # phone, and a correct "10,000" was invalid to the browser (and to screen readers)
        from subsync import ui
        tree = Tree(ui.number_field("files", "Files", "auto", help_text="h.", unit="files", keypad="text"))
        box = tree.all("input", id="files")[0]
        self.assertEqual((box["value"], box["inputmode"]), ("auto", "text"))
        self.assertEqual(tree.text_of["files-help"], "h.")
        for keypad, value in (("decimal", "2.5"), ("numeric", "10,000")):
            with self.subTest(keypad=keypad):
                box = Tree(ui.number_field("n", "N", value, keypad=keypad)).all("input", id="n")[0]
                self.assertEqual((box["value"], box["inputmode"]), (value, keypad))
        plain = Tree(ui.number_field("gb", "GB", "50", unit="GB")).all("input", id="gb")[0]
        self.assertEqual(plain["inputmode"], "numeric")
        for box in (tree.all("input", id="files")[0], plain):
            self.assertNotIn("pattern", box)

    def test_the_segmented_choice(self):
        from subsync import ui
        tree = Tree(ui.segmented("audio", "Audio per file", ["0.5", "1", "2", "4"], "1", unit="GB"))
        self.assertEqual([("checked" in a, a["value"]) for a in tree.all("input")],
                         [(False, "0.5"), (True, "1"), (False, "2"), (False, "4")])
        self.assertEqual(tree.all("div", role="radiogroup")[0]["aria-labelledby"], "audio-label")

    def test_check_rows_progress_and_changes(self):
        from subsync import ui
        checks = Tree(ui.check_rows([("ok", "Fine", None), ("warn", "Hm", "more"), ("bad", "No", None)]))
        self.assertEqual([a["aria-label"] for a in checks.all("span", role="img")], ["Done", "Warning", "Problem"])
        progress = Tree(ui.progress_list([("done", "A", None), ("now", "B", None), ("next", "C", None)]))
        self.assertEqual([a["aria-label"] for a in progress.all("span", role="img")], ["Done", "Working", "Waiting"])
        changes = Tree(ui.config_changes([("Your config", [("+", "adds subsync")], "Nothing else.")]))
        self.assertIn("adds subsync", changes.text())
        self.assertEqual(changes.all("span", **{"class": "sign"})[0]["aria-hidden"], "true")

    def test_the_config_changes_headings_are_one_level_under_the_steps(self):
        # Each config's heading was an h3 right under setup's step heading (an h1); level 2 now, the same look:
        # the setup page's own h2 size (.p-letter h2) comes before and is overridden, line height and spacing included
        from subsync import ui
        changes = ui.config_changes([("Your config", [("+", "x")], None), ("Its own", [("−", "y")], "z")])
        self.assertEqual(re.findall(r"<(h\d)>", changes), ["h2", "h2"])
        self.assertEqual(css_rule(".changes h2"),
                         "font-weight:700;font-size:17px;line-height:inherit;letter-spacing:inherit")
        self.assertNotIn(".changes h3", ui.APP_CSS)
        self.assertLess(ui.APP_CSS.index("\n.p-settings h2,.p-letter h2{"), ui.APP_CSS.index("\n.changes h2{"))

    def test_a_row_is_named_by_its_title_and_described_by_its_explanation(self):
        # The row is still the label (a click anywhere on it ticks it); its control is named by the title (and
        # tag) alone and described by the explanation, so a screen reader reads that once
        from subsync import ui
        text = ui.choice("c", "Title", "Help.", error_id="e")
        tree = Tree(text)
        box = tree.all("input", id="c")[0]
        self.assertTrue(text.startswith('<label class="choice"><input type="checkbox" id="c" '))
        self.assertEqual((box["aria-labelledby"], box["aria-describedby"]), ("c-title", "e c-help"))
        self.assertEqual((tree.all("b", id="c-title"), tree.text_of["c-title"], tree.text_of["c-help"]),
                         ([{"id": "c-title"}], "Title", "Help."))
        text = ui.radio_row("r", "v", "Title", "Help.", tag="(tag)", note="Note.")
        tree = Tree(text)
        button = tree.all("input", id="r-v")[0]
        self.assertTrue(text.startswith('<label class="way"><input type="radio" name="r" id="r-v" '))
        self.assertEqual((button["aria-labelledby"], button["aria-describedby"]), ("r-v-title", "r-v-help r-v-note"))
        self.assertEqual((tree.all("span", id="r-v-title")[0]["class"], tree.text_of["r-v-title"]),
                         ("head", "Title(tag)"))

    def test_a_show_button_is_named_by_its_box(self):
        # Every such button was named "Show", two or three alike on one page in a list of its controls or to
        # voice control; it names its box now, the visible word still Show (then Hide), no pressed state
        from subsync import pages, ui
        text = ui.field("pw", "Your AIOStreams password", "", kind="password", reveal=True)
        button = Tree(text).all("button")[0]
        self.assertEqual({k: v for k, v in button.items() if k.startswith(("aria-", "data-show", "data-hide"))},
                         {"aria-controls": "pw", "aria-label": "Show: Your AIOStreams password", "data-show": "Show",
                          "data-hide": "Hide", "data-show-label": "Show: Your AIOStreams password",
                          "data-hide-label": "Hide: Your AIOStreams password"})
        self.assertIn(">Show</button>", text)
        named = lambda page: [a["aria-label"] for a in Tree(page).all("button") if "data-reveal" in a]  # noqa: E731
        self.assertEqual(named(pages.signup_page(settings())), ["Show: Your AIOStreams password"])
        self.assertEqual(named(every_page()["settings-tmdb-personal"]),
                         ["Show: Your TMDB key", "Show: Your AIOStreams password"])
        self.assertEqual(named(pages.delete_confirm_page()), ["Show: Your AIOStreams password (optional)"])

    def test_the_update_panel(self):
        from subsync import ui
        tree = Tree(ui.update_panel("Updates", "About.", "Runs 1.0.0.", "1.1.0 ready.", "Update"))
        self.assertIn("1.1.0 ready.", tree.text())
        self.assertEqual(len(tree.all("button")), 1)
        self.assertFalse(Tree(ui.update_panel("Updates", "About.", "Runs.", None, "Update")).all("button"))

    def test_no_part_writes_a_style_attribute(self):
        from subsync import ui
        parts = [ui.radio_row("w", "a", "A", "a."), ui.step_frame(["A"], 0, "", "S"), ui.number_field("n", "N", "1"),
                 ui.segmented("s", "S", ["1"], "1"), ui.check_rows([("ok", "x", None)]),
                 ui.progress_list([("now", "x", None)]), ui.config_changes([("h", [("+", "x")], None)]),
                 ui.gauge("1 of 2", "files", .5), ui.update_panel("U", "a", "b", "c", "d")]
        for text in parts:
            self.assertNotRegex(text, r"\sstyle=")


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class FormLanguagesTest(unittest.TestCase):
    def test_the_slots_order_is_kept_whatever_lang_order_says(self):
        # R3-7: no page sends lang_order any more (it was the chips' picking order): the slots' order is the
        # order
        from subsync.signup import form_languages
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["eng", "rus"], "lang_order": "rus,eng"}), "eng, rus")
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["eng", "fre", "rus"], "lang_order": "rus"}),
                         "eng, fre, rus")
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["eng"], "lang_order": "zzz,eng,eng"}), "eng")

    def test_typed_languages_still_work(self):
        from subsync.signup import form_languages
        self.assertEqual(form_languages({"languages": "English, ru"}), "English, ru")
        self.assertIsNone(form_languages({}))

    def test_slots_in_their_order_none_left_out(self):
        from subsync.signup import form_languages
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["spa", "", "eng", ""]}), "spa, eng")
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["eng", "eng"]}), "eng")

    def test_no_chip_chosen(self):
        from subsync.signup import SignupError, form_languages
        with self.assertRaises(SignupError) as e:
            form_languages({"lang_ui": "1", "lang_order": "eng"})
        self.assertEqual(e.exception.field, "languages")


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class SignupMessagesTest(unittest.TestCase):
    def test_errors_name_their_field(self):
        from subsync.signup import SignupError, parse_link
        for text in ("", "http://a.test/stremio/x/y/manifest.json", "https://a.test/configure"):
            with self.subTest(text=text):
                with self.assertRaises(SignupError) as e:
                    parse_link(text)
                self.assertEqual(e.exception.field, "link")

    def test_a_wrong_password_in_plain_words(self):
        from subsync.aiosconfig import AiosError
        from subsync.signup import _plain
        for code in ("USER_INVALID_PASSWORD", "USER_INVALID_DETAILS", "USER_NOT_FOUND"):
            with self.subTest(code=code):
                e = AiosError("Reading the config: That config ID and password don't match a config on this AIOStreams.",
                              code=code)
                text = _plain(e, "Reading your AIOStreams config", own_password=True)
                self.assertEqual(text, "Your AIOStreams didn't accept this password for this link. Type the password "
                                       "of this config: the one you set on AIOStreams' configure page, not the one "
                                       "you sign in to AIOStreams with.")
                # the service's own sync config refused: not the person's password, so not that message
                self.assertNotIn("this password", _plain(e, "Updating AIOSubtitles' config on your AIOStreams"))


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.b = ServerHarness()
        self.b.start()

    def tearDown(self):
        self.b.close()

    def test_headers_allow_only_our_own_script_stylesheet_and_fonts(self):
        code, _, headers = http(self.b.base + "/")
        self.assertEqual(headers["Content-Security-Policy"],
                         "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; form-action 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        self.assertNotIn("X-Robots-Tag", headers)
        from subsync import ui
        for path, ctype, body_text in (("/static/app.js", "text/javascript", ui.APP_JS),
                                       ("/static/app.css", "text/css", ui.APP_CSS),
                                       (ui.JS_URL, "text/javascript", ui.APP_JS), (ui.CSS_URL, "text/css", ui.APP_CSS)):
            with self.subTest(path=path):
                code, body, headers = http(self.b.base + path)
                self.assertEqual(code, 200)
                self.assertTrue(headers["Content-Type"].startswith(ctype))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(body.decode(), body_text)
        for path in ("/static/other.js", "/static/fonts/other.ttf", "/static/fonts/../app.js", "/static/fonts/"):
            with self.subTest(path=path):
                self.assertEqual(http(self.b.base + path)[0], 404)

    def test_the_fonts_are_served_when_the_image_has_them(self):
        import shutil
        import tempfile
        from unittest import mock
        from subsync import ui
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(ui, "STATIC", empty):
            for name in ui.FONT_FILES:                     # a source checkout has none: 404, the page falls back
                self.assertEqual(http(f"{self.b.base}/static/fonts/{name}")[0], 404)
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "fonts"))
            for name in ui.FONT_FILES:
                with open(os.path.join(folder, "fonts", name), "wb") as f:
                    f.write(b"\x00\x01\x00\x00 font " + name.encode())
            shutil.copy(os.path.join(ui.STATIC, "app.css"), folder)
            with mock.patch.object(ui, "STATIC", folder):
                for name in ui.FONT_FILES:
                    with self.subTest(name=name):
                        code, body, headers = http(f"{self.b.base}/static/fonts/{name}")
                        self.assertEqual(code, 200)
                        self.assertEqual(headers["Content-Type"], "font/ttf")
                        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                        self.assertIn("max-age=31536000", headers["Cache-Control"])
                        self.assertTrue(body.endswith(name.encode()))

    def test_every_page_asks_a_proxy_in_front_to_pass_it_unchanged(self):
        # R3-10: Cloudflare added its analytics script to a page; with no-transform a proxy passes every page
        # (and the answer after a save) through as it is, whatever its own settings
        _, _, _, signed_up, token = self.b.sign_up()
        answers = {"signed up": signed_up}
        for path in ("/", "/privacy", f"/u/{token}/configure"):
            answers[path] = http(self.b.base + path)[2]
        code, _, answers["after a save"] = http(f"{self.b.base}/u/{token}/configure",
                                                form(action="languages", languages="English"))
        self.assertEqual(code, 303)
        for name, headers in answers.items():
            with self.subTest(page=name):
                self.assertEqual(headers["Cache-Control"], "no-store, no-transform")

    def test_personal_pages_send_noindex(self):
        _, _, text, headers, token = self.b.sign_up()
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex")
        self.assertEqual(http(f"{self.b.base}/u/{token}/configure")[2].get("X-Robots-Tag"), "noindex")

    def test_not_found_is_a_page_for_browsers_and_the_same_for_any_unknown_address(self):
        from subsync.users import new_token
        browser = {"Accept": "text/html,application/xhtml+xml"}
        a = http(f"{self.b.base}/u/{new_token()}/configure", headers=browser)
        b = http(f"{self.b.base}/nothing/here", headers=browser)
        c = http(f"{self.b.base}/u/{new_token()}/manifest.json", headers=browser)
        for code, body, headers in (a, b, c):
            self.assertEqual(code, 404)
            self.assertIn(b"This page doesn", body)
            self.assertEqual(headers["Cache-Control"], "no-store, no-transform")    # R3-10
        self.assertEqual(a[1], b[1])
        self.assertEqual(a[1], c[1])
        self.assertEqual(http(f"{self.b.base}/u/{new_token()}/manifest.json")[1], b"")     # apps: as before

    def test_signup_keeps_the_slots_order_whatever_lang_order_says(self):   # R3-7
        import json
        uuid = self.b.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        body = form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", password=PASSWORD, lang_ui="1",
                    lang_order="rus,eng", add_addon="on") + b"&lang=eng&lang=rus"
        code, text, _ = http(self.b.base + "/signup", body)
        self.assertEqual(code, 200, text)
        token = text.decode().split("https://subs.test/u/")[1][:43]
        self.assertEqual(self.b.users.open(token)["languages"], ["eng", "rus"])

    def test_signup_with_slots_keeps_their_order(self):
        import json
        uuid = self.b.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        body = form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", password=PASSWORD, lang_ui="1",
                    add_addon="on") + b"&lang=rus&lang=&lang=eng&lang="
        code, text, _ = http(self.b.base + "/signup", body)
        self.assertEqual(code, 200, text)
        token = text.decode().split("https://subs.test/u/")[1][:43]
        self.assertEqual(self.b.users.open(token)["languages"], ["rus", "eng"])

    def test_more_languages_than_the_limit_are_refused_at_sign_up_and_on_the_settings_page(self):
        self.b.close()
        self.b = ServerHarness()
        self.b.start(SUBSYNC_MAX_LANGUAGES="2")
        code, body, _ = http(self.b.base + "/signup", form(link=f"{INSTANCE}/stremio/x/y/manifest.json",
                                                           password=PASSWORD, lang_ui="1") + b"&lang=eng&lang=rus&lang=spa")
        self.assertEqual(code, 400)
        self.assertIn(b"Two languages at most", body)
        self.assertEqual(self.b.fake.requests, [])
        _, _, _, _, token = self.b.sign_up()
        code, body, _ = http(f"{self.b.base}/u/{token}/configure", form(action="languages", lang_ui="1") +
                             b"&lang=eng&lang=rus&lang=spa")
        self.assertEqual(code, 400)
        self.assertIn(b"Two languages at most", body)
        self.assertEqual(self.b.users.open(token)["languages"], ["eng"])

    def test_a_refresh_to_stored_copies_says_so(self):
        _, _, _, _, token = self.b.sign_up()
        record = self.b.users.open(token)
        main = self.b.fake.configs[record["main_uuid"]]["config"]
        main["services"] = [{"id": "realdebrid", "enabled": True, "credentials": {"apiKey": "rd"}}]
        code, _, headers = http(f"{self.b.base}/u/{token}/configure", form(action="refresh", password=PASSWORD))
        self.assertEqual((code, headers["Location"]), (303, "configure?done=to-stored"))
        code, body, _ = http(f"{self.b.base}/u/{token}/configure?done=to-stored")
        self.assertIn("removed its own config from your AIOStreams and offers stored copies only", body.decode())
        self.assertIn("Stored copies only", body.decode())

    def test_signup_with_no_chip_chosen_contacts_nothing(self):
        code, body, _ = http(self.b.base + "/signup", form(link=f"{INSTANCE}/stremio/x/y/manifest.json",
                                                           password=PASSWORD, lang_ui="1", lang_order="eng"))
        self.assertEqual(code, 400)
        self.assertIn(b"Choose at least one language", body)
        self.assertEqual(self.b.fake.requests, [])

    def test_a_wrong_password_is_marked_at_the_field(self):
        import json
        uuid = self.b.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        code, body, _ = http(self.b.base + "/signup", form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json",
                                                           password="wrong", lang_ui="1", lang="eng", lang_order="eng"))
        text = body.decode()
        tree = Tree(text)
        self.assertEqual(code, 400)
        self.assertIn("didn't accept this password", tree.text_of["problem-message"])
        self.assertEqual(tree.all("input", name="password")[0].get("aria-invalid"), "true")
        self.assertIn("problem-message", tree.all("input", name="password")[0]["aria-describedby"])
        self.assertNotIn(f"stremio/{uuid}", text)
        self.assertNotIn('value="wrong"', text)

    def test_a_link_that_cant_be_read_is_marked_at_its_box_not_a_blank_page(self):
        # A port that isn't a number, or an unmatched "[", ended the sign-up and Refresh in a blank
        # error page
        from subsync.signup import NOT_A_LINK
        uuid = "1a2b3c4d-1111-2222-3333-444455556666"
        for link in (f"https://aios.test:abc/stremio/{uuid}/ENCRYPTED/manifest.json",
                     "https://[aios.test/stremio/u/Family.TV/manifest.json"):
            with self.subTest(link=link):
                code, body, _ = http(self.b.base + "/signup", form(link=link, password=PASSWORD, languages="English"))
                tree = Tree(body.decode())
                self.assertEqual(code, 400)
                self.assertEqual(tree.text_of["problem-message"], NOT_A_LINK)
                self.assertEqual(tree.all("input", name="link")[0].get("aria-invalid"), "true")
        _, _, _, _, token = self.b.sign_up()
        self.b.fake.requests.clear()
        code, body, _ = http(f"{self.b.base}/u/{token}/configure",
                             form(action="refresh", password=PASSWORD, link=f"https://aios.test:abc/stremio/{uuid}/E/"
                                                                             "manifest.json"))
        tree = Tree(body.decode())
        self.assertEqual(code, 400)
        self.assertEqual(tree.text_of["new_link-error"], NOT_A_LINK)
        self.assertEqual(self.b.fake.requests, [])

    def test_an_unreachable_aiostreams_on_refresh_marks_the_new_link_box_only_when_one_was_typed(self):
        # With the optional box left empty, the error about the stored AIOStreams was shown under it and the
        # box marked wrong
        from subsync.aiosconfig import AiosApi
        _, _, _, _, token = self.b.sign_up()
        self.b.service.api_factory = lambda base, timeout: AiosApi("https://127.0.0.1:9", timeout=timeout,
                                                                   max_timeout=timeout, retry_wait_cap=5)
        url = f"{self.b.base}/u/{token}/configure"
        reach = "Reading your AIOStreams config: AIOSubtitles can't reach your AIOStreams from the internet."
        code, body, _ = http(url, form(action="refresh", password=PASSWORD))
        tree = Tree(body.decode())
        self.assertEqual(code, 400)
        self.assertTrue(tree.text_of["problem-message"].startswith(reach))   # at the top only
        self.assertEqual(tree.all("a", href="#new_link"), [])
        self.assertNotIn("new_link-error", tree.text_of)
        self.assertNotIn("aria-invalid", tree.all("input", name="link")[0])
        uuid = self.b.users.open(token)["main_uuid"]
        code, body, _ = http(url, form(action="refresh", password=PASSWORD,
                                       link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json"))
        tree = Tree(body.decode())
        self.assertEqual(code, 400)
        self.assertTrue(tree.text_of["new_link-error"].startswith(reach))    # a link typed: at its box
        self.assertEqual(tree.all("input", name="link")[0].get("aria-invalid"), "true")

    def test_an_unticked_box_deletes_the_kept_password_even_when_the_refresh_fails(self):
        # After a failed Refresh the password stayed kept, and the page showed the box ticked again with "Stop
        # keeping my password"
        self.b.close()
        self.b = ServerHarness()
        self.b.start(SUBSYNC_RECHECK_HOURS="24")
        _, _, _, _, token = self.b.sign_up(keep_password="on")
        self.assertEqual(self.b.users.open(token)["auto_password"], PASSWORD)
        code, body, _ = http(f"{self.b.base}/u/{token}/configure", form(action="refresh", password="not-the-password"))
        text = body.decode()
        tree = Tree(text)
        self.assertEqual(code, 400)
        self.assertIn("didn't accept this password", tree.text_of["problem-message"])
        self.assertNotIn("auto_password", self.b.users.open(token))
        self.assertNotIn("checked", tree.all("input", name="keep_password")[0])
        self.assertNotIn('value="forget-password"', text)

    def test_saving_languages_redirects_and_says_so_once(self):
        _, _, _, _, token = self.b.sign_up()
        url = f"{self.b.base}/u/{token}/configure"
        code, body, headers = http(url, form(action="languages", lang_ui="1", lang="eng", lang_order="eng") +
                                   b"&lang=rus")
        self.assertEqual(code, 303)
        self.assertEqual(headers["Location"], "configure?done=languages")
        self.assertEqual(self.b.users.open(token)["languages"], ["eng", "rus"])
        code, body, _ = http(url + "?done=languages")
        self.assertIn("Languages saved: English, Russian.", body.decode())
        self.assertNotIn("Languages saved", http(url + "?done=nonsense")[1].decode())

    def test_a_refresh_redirects_so_a_reload_never_sends_the_password_again(self):
        _, _, _, _, token = self.b.sign_up()
        url = f"{self.b.base}/u/{token}/configure"
        code, _, headers = http(url, form(action="refresh", password=PASSWORD))
        self.assertEqual((code, headers["Location"]), (303, "configure?done=refreshed"))

    def test_a_refresh_with_a_wrong_password_stays_on_the_page_with_the_error_at_the_field(self):
        _, _, _, _, token = self.b.sign_up()
        code, body, _ = http(f"{self.b.base}/u/{token}/configure", form(action="refresh", password="wrong"))
        self.assertEqual(code, 400)
        self.assertEqual(Tree(body.decode()).all("input", id="refresh")[0].get("aria-invalid"), "true")

    def test_a_wrong_password_on_the_delete_step_stays_on_the_delete_step(self):
        _, _, _, _, token = self.b.sign_up()
        code, body, _ = http(f"{self.b.base}/u/{token}/configure", form(action="delete", password="wrong"))
        self.assertEqual(code, 400)
        self.assertIn('value="delete"', body.decode())
        self.assertIn("Nothing was deleted", body.decode())
        self.assertIsNotNone(self.b.users.open(token))

    def test_too_many_attempts_says_when(self):
        # the hour's attempts from one address, each refused as a form error, then the limit and when to try again
        # (R3-5,: test_public_http's copy of this scenario, which checked the 400s, merged in here)
        for _ in range(self.b.s.signups_per_hour):
            self.assertEqual(http(self.b.base + "/signup", form(link="", password="x"))[0], 400)
        code, body, _ = http(self.b.base + "/signup", form(link="", password="x"))
        self.assertEqual(code, 429)
        self.assertIn(b"Try again in an hour.", body)

    def test_after_a_problem_the_boxes_are_as_sent_and_the_note_only_where_a_retry_helps(self):
        # The boxes came back as on a fresh page;: the layer opened past the emptied link, and "enter
        # them again" followed too many attempts too
        import json
        from subsync import explain
        note = explain.PUBLIC_TEXTS["kept_note"]
        b = ServerHarness()      # an instance that shows all four boxes, with eight attempts an hour
        b.start(SUBSYNC_EXTRA_SOURCES="osv3", SUBSYNC_RECHECK_HOURS="24", SUBSYNC_USENET="on",
                SUBSYNC_USENET_LISTINGS_PER_DAY="5", SUBSYNC_SIGNUPS_PER_HOUR="8")
        self.addCleanup(b.close)
        uuid = b.fake.add(json.loads(json.dumps(VIEWER)), "the-password")
        link, wrong = f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "not-the-password-0435"
        for sent in ({}, {"add_addon": "on"}, {"add_addon": "on", "clean_up": "on", "usenet": "on"},
                     {"keep_password": "on", "usenet": "on"}, {"add_addon": "yes", "keep_password": ""}):
            with self.subTest(sent=sent):
                code, body, _ = http(b.base + "/signup", form(link=link, password=wrong, languages="English", **sent))
                self.assertEqual(code, 400)
                tree = Tree(body.decode())
                self.assertIn("didn't accept this password", tree.text_of["problem-message"])
                self.assertEqual({name: "checked" in tree.all("input", id=name)[0] for name in BOXES},
                                 {name: sent.get(name) == "on" for name in BOXES})     # ticked is "on", as signup reads
                self.assertEqual(tree.all("body")[0]["data-open-at"], "link")
                self.assertIn(note, tree.text_of["problem"])
                self.assertNotIn(wrong.encode(), body)                          # the password isn't sent back
        while True:                       # the hour's attempts used up: trying again only costs a try
            code, body, _ = http(b.base + "/signup", form(link=link, password=wrong, languages="English"))
            if code == 429:
                break
            self.assertEqual(code, 400)
        problem = Tree(body.decode()).text_of["problem"]
        self.assertIn("Too many sign-up attempts", problem)
        self.assertNotIn(note, problem)

    def test_a_full_instance_asks_for_nothing_again(self):
        # "enter them again" followed "AIOSubtitles is full…", where a retry only costs a try
        from subsync import explain
        full = ServerHarness()
        full.start(SUBSYNC_MAX_USERS="0")
        try:
            code, body, _ = http(full.base + "/signup", form(link="x", password="x", languages="English"))
        finally:
            full.close()
        self.assertEqual(code, 503)
        problem = Tree(body.decode()).text_of["problem"]
        self.assertIn("AIOSubtitles is full for now.", problem)
        self.assertNotIn(explain.PUBLIC_TEXTS["kept_note"], problem)

    def test_an_unexpected_failure_gives_a_browser_the_error_page_and_an_app_nothing(self):
        # A browser got an empty 500, a blank page with no way back; the "Something went wrong" page was never
        # shown
        import sqlite3
        from unittest import mock
        from subsync import explain, pages
        browser = {"Accept": "text/html,application/xhtml+xml"}
        line = explain.PUBLIC_TEXTS["went_wrong"]
        _, _, _, _, token = self.b.sign_up()
        broken = mock.Mock(side_effect=sqlite3.OperationalError("database is locked"))
        cases = (("/privacy", None, mock.patch.object(pages, "privacy_page", broken), False),
                 (f"/u/{token}/configure", None, mock.patch.object(pages, "settings_page", broken), True),
                 (f"/u/{token}/configure", form(action="languages", languages="English"),
                  mock.patch("subsync.public.set_languages", broken), True),
                 ("/signup", form(link="x", password="x", languages="English"),
                  mock.patch("subsync.public.signup", broken), False))
        for path, data, patch, personal in cases:
            with self.subTest(path=path, posted=bool(data)), patch:
                code, body, headers = http(self.b.base + path, data, headers=browser)
                self.assertEqual(code, 500)
                text = Tree(body.decode()).text()
                self.assertIn("Something went wrong " + line + " Back to the start page", text)
                self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(headers["Cache-Control"], "no-store, no-transform")
                self.assertEqual(headers.get("X-Robots-Tag"), "noindex" if personal else None)
                self.assertEqual(b'<meta name="robots" content="noindex">' in body, personal)
                self.assertEqual(http(self.b.base + path, data)[:2], (500, b""))         # an app: nothing, as before
        self.assertEqual(self.b.logs.lines.count("request failed: OperationalError"), 8)


if __name__ == "__main__":
    unittest.main()
