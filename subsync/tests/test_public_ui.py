"""Public mode's pages: the look (the approved tokens, contrast, motion, the breakpoints, two typefaces, no
outside resources and no inline style), each page's structure, every text an approved one, errors tied to their field,
the language limit, stored copies only, the small script, the installer's parts, and the routes around the pages
(redirect after a save, the not-found page, noindex, the security header, the stylesheet, script and fonts)."""
import html.parser
import os
import re
import sys
import types
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync.settings import Settings  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False
NEEDS = "cryptography is not installed (the image has it; tests run it in a venv)"


KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
BASE = "https://subs.test"
TOKEN = "T" * 43
LINK = f"{BASE}/u/{TOKEN}/manifest.json"


def settings(**env):
    return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PUBLIC_BASE": BASE}, **env))


def contrast(a, b):
    def lum(h):
        r, g, b_ = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b_)
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Tree(html.parser.HTMLParser):
    """Every element with its attributes, the text inside each id'd element, and the page's text."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements, self.text_of, self.open_ids, self.words = [], {}, [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.elements.append((tag, a))
        if "id" in a and tag not in ("input", "br", "img", "meta", "link"):
            self.open_ids.append((tag, a["id"]))
            self.text_of[a["id"]] = ""

    def handle_endtag(self, tag):
        for i in range(len(self.open_ids) - 1, -1, -1):
            if self.open_ids[i][0] == tag:
                del self.open_ids[i]
                break

    def handle_data(self, data):
        self.words.append(data)
        for _, i in self.open_ids:
            self.text_of[i] += data

    def all(self, tag, **match):
        return [a for t, a in self.elements if t == tag and all(a.get(k) == v for k, v in match.items())]

    def text(self):
        return re.sub(r"\s+", " ", " ".join(self.words))


def result(**kw):
    # the fields of signup.SignupResult, without importing signup (it needs cryptography), so the page tests run anywhere
    base = dict(token=TOKEN, manifest_url=LINK, changes=["adds 1 subtitle source: AIOSubtitles"], warning=None,
                added=True, manual=False, languages=["eng", "spa"], store_only=False, to_store_only=False,
                usenet=False, usenet_why=None, sync_made=True)
    base.update(kw)
    return types.SimpleNamespace(**base)


RECORD = {"languages": ["eng", "spa"], "synced_at": 0}
USAGE = {"files": 3, "bytes": 351 * 1024}
HOSTILE = "<b onclick=\"x\">&'\""


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
        "result": pages.result_page(result()),
        "result-manual": pages.result_page(result(added=False, manual=True, changes=[])),
        "result-unticked": pages.result_page(result(added=False, manual=False, changes=[])),
        "result-rd": pages.result_page(result(warning=explain.PUBLIC_REAL_DEBRID_WARNING)),
        "result-stored": pages.result_page(result(store_only=True)),
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
        "not-found": pages.not_found_page(),
        "error": pages.error_page("Wrong."),
    }
    for code in pages.DONE:
        out[f"settings-done-{code}"] = pages.settings_page(s, RECORD, USAGE, LINK, now=0,
                                                           message=pages.done_message(code, RECORD)[0],
                                                           notice=pages.done_message(code, RECORD)[1])
    for state in explain.PUBLIC_STATES:
        out[f"settings-state-{state}"] = pages.settings_page(s, dict(RECORD, state=state, state_detail="Detail."),
                                                             USAGE, LINK, now=0)
    return out


def approved_corpus():
    """Every approved text the pages may show, formatted with the made-up values every_page uses."""
    from subsync import explain, pages
    from subsync.aiosconfig import LANGUAGES
    texts = list(explain.PUBLIC_FIELDS.values()) + list(explain.PUBLIC_BEFORE) + list(explain.PUBLIC_MANUAL_STEPS)
    texts += list(explain.PUBLIC_DELETE_LIST.values()) + list(explain.PUBLIC_STORE_ONLY) + list(explain.PUBLIC_HEADLINE)
    texts += [explain.PUBLIC_REAL_DEBRID_NOTICE, explain.CONTACT] + [explain.languages_help(n) for n in (1, 2, 3, 4)]
    texts.append(explain.PUBLIC_MANUAL_LEAD)
    texts += [h for h, _ in explain.PRIVACY_PARTS]
    texts += [(t[:1].upper() + t[1:]) for _, t in explain.PRIVACY_PARTS]
    audio = settings(SUBSYNC_AUDIO_ROUTE="on")            # the audio route's words, as an instance with it on shows them
    texts += list(explain.intro_parts(audio)) + [explain.next_text(audio)]
    texts += [(t[:1].upper() + t[1:]) for _, t in explain.privacy_parts(audio)]
    # the original-language lookups' words, by what the instance uses (TMDB with its key, each person's, Wikidata
    # only, none), and TMDB's notice and its logo's name
    for env in ({"SUBSYNC_TMDB_KEY": "k"}, {"SUBSYNC_TMDB_PERSONAL": "on"}, {}, {"SUBSYNC_ORIGINAL_LANGUAGE": "off"},
                {"SUBSYNC_TMDB_KEY": "k", "SUBSYNC_AUDIO_ROUTE": "on"}):
        texts += [(t[:1].upper() + t[1:]) for _, t in explain.privacy_parts(settings(**env))]
    texts.append(explain.TMDB_NOTICE)
    for title, text, _ in explain.PUBLIC_STATES.values():
        texts += [title, text.replace("{detail}", "Detail.")]
    for key, text in explain.PUBLIC_TEXTS.items():
        texts.append(text.format(n=5, when="at sign-up", names="English, Spanish", answer="Off") if "{" in text else text)
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
    texts += [name for name, _, _ in LANGUAGES] + list(pages.UI_WORDS)
    return "\n".join(texts)


DYNAMIC = re.compile(r"^(\d+ of \d+|[\d.]+ MB of \d+ MB|English, Spanish(, French)?|Wrong\.|Detail\.|https://subs\.test/\S*"
                     r"|Changed in your AIOStreams config: adds 1 subtitle source: AIOSubtitles|Remove: \w+)$")


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
        for name in ("aria-label", "placeholder", "data-show", "data-hide", "data-done"):
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
        corpus = approved_corpus()
        for name, text in every_page().items():
            for piece in Visible(text).found:
                with self.subTest(page=name, text=piece[:60]):
                    self.assertTrue(piece in corpus or DYNAMIC.match(piece), piece)

    def test_personal_pages_ask_not_to_be_indexed(self):
        pages = every_page()
        for name in ("result", "settings", "delete", "deleted"):
            self.assertIn('<meta name="robots" content="noindex">', pages[name])
        for name in ("signup", "privacy", "not-found"):
            self.assertNotIn("noindex", pages[name])

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

    def test_the_language_slots_follow_the_instance_limit(self):
        from subsync import explain, pages
        for limit in (1, 2, 3, 4):
            with self.subTest(limit=limit):
                text = pages.signup_page(settings(SUBSYNC_MAX_LANGUAGES=str(limit)))
                tree = Tree(text)
                slots = tree.all("select", name="lang")
                self.assertEqual(len(slots), limit)
                self.assertEqual(tree.all("div", id="languages")[0]["data-max-languages"], str(limit))
                self.assertIn(explain.languages_help(limit), tree.text())
                labels = [a["for"] for a in tree.all("label") if a.get("for", "").startswith("lang-")]
                self.assertEqual(labels, [f"lang-{i}" for i in range(1, limit + 1)])

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
    def test_the_stand_in_has_the_real_results_fields(self):
        try:
            from subsync.signup import SignupResult
        except ImportError:
            self.skipTest(NEEDS)
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
        self.assertIn("0.3 MB of 10000 MB", text)
        self.assertIn("Resets at 00:00 UTC, in 23 h.", text)

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

    def test_after_a_problem_the_answer_is_kept_and_the_layer_opens_at_its_screen(self):
        from subsync import pages
        text = pages.signup_page(self.on(), error="Wrong.", field="audio", audio="open")
        self.assertEqual([v for v, chosen in self.radios(text) if chosen], ["open"])
        tree = Tree(text)
        self.assertEqual(tree.all("body")[0]["data-open-at"], "config")
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
        self.assertEqual(len(tree.all("h2")), len(explain.PRIVACY_PARTS) + 1)
        default = {"audio": "", "tmdb": "", "lookup": explain.PRIVACY_LOOKUP["wikidata"]}   # what settings() fills in
        for head, text in explain.PRIVACY_PARTS:
            text = text.format(**default)
            self.assertIn(head, tree.text())
            self.assertIn(re.sub(r"\s+", " ", text[:1].upper() + text[1:]), tree.text())

    def test_not_found_page(self):
        from subsync import explain, pages
        text = Tree(pages.not_found_page()).text()
        self.assertIn("This page doesn't exist", text)
        self.assertIn(explain.PUBLIC_TEXTS["not_found"], text)


class ScriptTest(unittest.TestCase):
    def test_the_script_is_small_local_and_respects_reduced_motion(self):
        from subsync.ui import APP_JS
        self.assertLess(len(APP_JS.encode()), 16 * 1024)
        self.assertIn("prefers-reduced-motion", APP_JS)
        self.assertNotRegex(APP_JS, r"https?://|\beval\(|new Function|innerHTML|outerHTML|insertAdjacentHTML|"
                                    r"document\.write|localStorage|sessionStorage|fetch\(|XMLHttpRequest|sendBeacon|"
                                    r"setAttribute\(\s*['\"]style")
        self.assertNotRegex(APP_JS, r"\bAD\d{1,3}\b|S4-\d+")

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

    def test_the_number_field_takes_auto(self):
        from subsync import ui
        tree = Tree(ui.number_field("files", "Files", "auto", help_text="h.", unit="files", auto=True))
        box = tree.all("input", id="files")[0]
        self.assertEqual((box["value"], box["pattern"], box["inputmode"]), ("auto", "auto|[0-9]+", "text"))
        self.assertEqual(tree.text_of["files-help"], "h.")
        plain = Tree(ui.number_field("gb", "GB", "50", unit="GB")).all("input", id="gb")[0]
        self.assertEqual((plain["pattern"], plain["inputmode"]), ("[0-9]+", "numeric"))

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
        progress = Tree(ui.progress_list([("done", "A"), ("now", "B"), ("next", "C")]))
        self.assertEqual([a["aria-label"] for a in progress.all("span", role="img")], ["Done", "Working", "Waiting"])
        changes = Tree(ui.config_changes([("Your config", [("+", "adds subsync")], "Nothing else.")]))
        self.assertIn("adds subsync", changes.text())
        self.assertEqual(changes.all("span", **{"class": "sign"})[0]["aria-hidden"], "true")

    def test_the_update_panel(self):
        from subsync import ui
        tree = Tree(ui.update_panel("Updates", "About.", "Runs <b>1.0.0</b>.", "1.1.0 ready.", "Update"))
        self.assertIn("1.1.0 ready.", tree.text())
        self.assertEqual(len(tree.all("button")), 1)
        self.assertFalse(Tree(ui.update_panel("Updates", "About.", "Runs.", None, "Update")).all("button"))

    def test_no_part_writes_a_style_attribute(self):
        from subsync import ui
        parts = [ui.radio_row("w", "a", "A", "a."), ui.step_frame(["A"], 0, "", "S"), ui.number_field("n", "N", "1"),
                 ui.segmented("s", "S", ["1"], "1"), ui.check_rows([("ok", "x", None)]),
                 ui.progress_list([("now", "x")]), ui.config_changes([("h", [("+", "x")], None)]),
                 ui.gauge("1 of 2", "files", .5), ui.update_panel("U", "a", "b", "c", "d")]
        for text in parts:
            self.assertNotRegex(text, r"\sstyle=")


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class FormLanguagesTest(unittest.TestCase):
    def test_chips_in_the_order_picked(self):
        from subsync.signup import form_languages
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["eng", "rus"], "lang_order": "rus,eng"}), "rus, eng")
        # without the script the order field is what the page showed: new boxes follow in the page's order
        self.assertEqual(form_languages({"lang_ui": "1", "lang": ["eng", "fre", "rus"], "lang_order": "rus"}),
                         "rus, eng, fre")
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
        from test_public_http import Base
        self.b = Base()
        self.b.start()

    def tearDown(self):
        self.b.tearDown()

    def test_headers_allow_only_our_own_script_stylesheet_and_fonts(self):
        from test_public_http import http
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
        from test_public_http import http
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

    def test_personal_pages_send_noindex(self):
        from test_public_http import http
        _, _, text, headers, token = self.b.sign_up()
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex")
        self.assertEqual(http(f"{self.b.base}/u/{token}/configure")[2].get("X-Robots-Tag"), "noindex")

    def test_not_found_is_a_page_for_browsers_and_the_same_for_any_unknown_address(self):
        from subsync.users import new_token
        from test_public_http import http
        browser = {"Accept": "text/html,application/xhtml+xml"}
        a = http(f"{self.b.base}/u/{new_token()}/configure", headers=browser)
        b = http(f"{self.b.base}/nothing/here", headers=browser)
        c = http(f"{self.b.base}/u/{new_token()}/manifest.json", headers=browser)
        for code, body, headers in (a, b, c):
            self.assertEqual(code, 404)
            self.assertIn(b"This page doesn", body)
            self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(a[1], b[1])
        self.assertEqual(a[1], c[1])
        self.assertEqual(http(f"{self.b.base}/u/{new_token()}/manifest.json")[1], b"")     # apps: as before

    def test_signup_with_chips_keeps_the_order(self):
        from test_public_http import INSTANCE, PASSWORD, VIEWER, form, http
        import json
        uuid = self.b.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        body = form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", password=PASSWORD, lang_ui="1",
                    lang_order="rus,eng", add_addon="on") + b"&lang=eng&lang=rus"
        code, text, _ = http(self.b.base + "/signup", body)
        self.assertEqual(code, 200, text)
        token = text.decode().split("https://subs.test/u/")[1][:43]
        self.assertEqual(self.b.users.open(token)["languages"], ["rus", "eng"])

    def test_signup_with_slots_keeps_their_order(self):
        from test_public_http import INSTANCE, PASSWORD, VIEWER, form, http
        import json
        uuid = self.b.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        body = form(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", password=PASSWORD, lang_ui="1",
                    add_addon="on") + b"&lang=rus&lang=&lang=eng&lang="
        code, text, _ = http(self.b.base + "/signup", body)
        self.assertEqual(code, 200, text)
        token = text.decode().split("https://subs.test/u/")[1][:43]
        self.assertEqual(self.b.users.open(token)["languages"], ["rus", "eng"])

    def test_more_languages_than_the_limit_are_refused_at_sign_up_and_on_the_settings_page(self):
        from test_public_http import INSTANCE, PASSWORD, form, http
        self.b.tearDown()
        self.b = __import__("test_public_http").Base()
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
        from test_public_http import PASSWORD, form, http
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
        from test_public_http import INSTANCE, PASSWORD, form, http
        code, body, _ = http(self.b.base + "/signup", form(link=f"{INSTANCE}/stremio/x/y/manifest.json",
                                                           password=PASSWORD, lang_ui="1", lang_order="eng"))
        self.assertEqual(code, 400)
        self.assertIn(b"Choose at least one language", body)
        self.assertEqual(self.b.fake.requests, [])

    def test_a_wrong_password_is_marked_at_the_field(self):
        from test_public_http import INSTANCE, PASSWORD, VIEWER, form, http
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

    def test_saving_languages_redirects_and_says_so_once(self):
        from test_public_http import form, http
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
        from test_public_http import PASSWORD, form, http
        _, _, _, _, token = self.b.sign_up()
        url = f"{self.b.base}/u/{token}/configure"
        code, _, headers = http(url, form(action="refresh", password=PASSWORD))
        self.assertEqual((code, headers["Location"]), (303, "configure?done=refreshed"))

    def test_a_refresh_with_a_wrong_password_stays_on_the_page_with_the_error_at_the_field(self):
        from test_public_http import form, http
        _, _, _, _, token = self.b.sign_up()
        code, body, _ = http(f"{self.b.base}/u/{token}/configure", form(action="refresh", password="wrong"))
        self.assertEqual(code, 400)
        self.assertEqual(Tree(body.decode()).all("input", id="refresh")[0].get("aria-invalid"), "true")

    def test_a_wrong_password_on_the_delete_step_stays_on_the_delete_step(self):
        from test_public_http import form, http
        _, _, _, _, token = self.b.sign_up()
        code, body, _ = http(f"{self.b.base}/u/{token}/configure", form(action="delete", password="wrong"))
        self.assertEqual(code, 400)
        self.assertIn('value="delete"', body.decode())
        self.assertIn("Nothing was deleted", body.decode())
        self.assertIsNotNone(self.b.users.open(token))

    def test_too_many_attempts_says_when(self):
        from test_public_http import form, http
        for _ in range(5):
            http(self.b.base + "/signup", form(link="", password="x"))
        code, body, _ = http(self.b.base + "/signup", form(link="", password="x"))
        self.assertEqual(code, 429)
        self.assertIn(b"Try again in an hour.", body)


if __name__ == "__main__":
    unittest.main()
