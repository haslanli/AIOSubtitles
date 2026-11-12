"""Public mode's pages: the look (contrast, motion, no outside resources), each page's structure and texts, errors tied
to their field, the small script, and the routes around the pages (redirect after a save, the not-found page, noindex,
the script's own address)."""
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
                added=True, manual=False, languages=["eng", "rus"])
    base.update(kw)
    return types.SimpleNamespace(**base)


def every_page():
    from subsync import pages
    s = settings(SUBSYNC_PUBLIC_INVITE="code")
    record = {"languages": ["eng", "spa"]}
    usage = {"files": 3, "bytes": 351 * 1024}
    return {"signup": pages.signup_page(s), "signup-error": pages.signup_page(s, error="Wrong.", field="password"),
            "result": pages.result_page(result()), "result-manual": pages.result_page(result(added=False, manual=True,
                                                                                          changes=[], warning="RD")),
            "settings": pages.settings_page(s, record, usage, LINK, now=0),
            "delete": pages.delete_confirm_page(), "deleted": pages.deleted_page({}),
            "privacy": pages.privacy_page(s), "not-found": pages.not_found_page(), "error": pages.error_page("Oops.")}


class LookTest(unittest.TestCase):
    def test_every_colour_pair_passes_aa_in_light_and_dark(self):
        # Text 4.5:1, field edges and other parts people must see 3:1 (WCAG 2.1, 1.4.3 and 1.4.11).
        from subsync.pages import TOKENS
        pairs = [("ink", "paper", 4.5), ("ink", "surface", 4.5), ("ink-2", "paper", 4.5), ("ink-2", "surface", 4.5),
                 ("link", "paper", 4.5), ("link", "surface", 4.5), ("on-ink", "ink", 4.5), ("on-mark", "mark", 4.5),
                 ("on-danger", "danger", 4.5), ("ok-ink", "ok-bg", 4.5), ("warn-ink", "warn-bg", 4.5),
                 ("err-ink", "err-bg", 4.5), ("field", "paper", 3), ("field", "surface", 3), ("mark", "paper", 3),
                 ("mark", "surface", 3), ("focus", "paper", 3), ("focus", "surface", 3)]
        for scheme in ("light", "dark"):
            for fg, bg, need in pairs:
                with self.subTest(scheme=scheme, pair=(fg, bg)):
                    self.assertGreaterEqual(contrast(TOKENS[scheme][fg], TOKENS[scheme][bg]), need)

    def test_both_colour_schemes_are_in_the_style(self):
        from subsync.pages import STYLE, TOKENS
        self.assertEqual(set(TOKENS["light"]), set(TOKENS["dark"]))
        self.assertIn("@media (prefers-color-scheme: dark)", STYLE)
        for scheme in ("light", "dark"):
            for name, value in TOKENS[scheme].items():
                self.assertIn(f"--{name}:{value}", STYLE.replace(" ", ""))

    def test_reduced_motion_stops_every_animation(self):
        from subsync.pages import STYLE
        block = STYLE.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("}\n}", 1)[0]
        for rule in ("animation: none", "transition: none"):
            self.assertIn(rule, block)

    def test_type_scale_and_spacing_are_fluid(self):
        from subsync.pages import STYLE
        self.assertGreaterEqual(STYLE.count("clamp("), 8)
        self.assertNotRegex(STYLE, r"width:\s*\d{3,}px")        # no fixed widths that could overflow a phone

    def test_no_outside_resources_and_one_own_script(self):
        for name, text in every_page().items():
            with self.subTest(page=name):
                self.assertNotRegex(text, r"<link\b|@import|url\(\s*['\"]?https?:|<iframe|<img\b")
                self.assertEqual(re.findall(r"<script\b[^>]*>", text), ['<script src="/static/app.js" defer>'])
                self.assertNotRegex(text, r"<script\b[^>]*>[^<]+</script>")      # nothing inline
                self.assertNotRegex(text, r"\son[a-z]+=")                       # no inline event handlers
                for url in re.findall(r"https?://[^\s\"'<>]+", text):
                    # the SVG namespace, our own address, the questions page, and the link field's example text
                    self.assertTrue(url.startswith(("http://www.w3.org/2000/svg", BASE, "https://github.com/",
                                                    "https://…")), url)

    def test_every_page_has_one_main_heading_landmarks_and_a_language(self):
        for name, text in every_page().items():
            with self.subTest(page=name):
                tree = Tree(text)
                self.assertEqual(len(tree.all("h1")), 1)
                self.assertEqual(len(tree.all("main")), 1)
                self.assertEqual(len(tree.all("header")), 1)
                self.assertEqual(len(tree.all("footer")), 1)
                self.assertTrue(tree.all("html", lang="en"))
                ids = [a["id"] for _, a in tree.elements if "id" in a]
                self.assertEqual(len(ids), len(set(ids)), "an id used twice")

    def test_every_field_has_a_label_and_its_explanation(self):
        for name, text in every_page().items():
            tree = Tree(text)
            labelled = {a.get("for") for a in tree.all("label")}
            wrapped = text.count("<label class=\"chip\">")
            for tag in ("input", "select", "textarea"):
                for a in tree.all(tag):
                    if a.get("type") in ("hidden", "submit"):
                        continue
                    with self.subTest(page=name, field=a.get("name"), id=a.get("id")):
                        if a.get("type") == "checkbox" and a.get("name") == "lang":
                            self.assertGreater(wrapped, 0)       # a chip: the label wraps the box
                        else:
                            self.assertIn(a.get("id"), labelled)
                        for ref in (a.get("aria-describedby") or "").split():
                            self.assertIn(ref, tree.text_of, f"{ref} is not on the page")
                        if a.get("type") != "checkbox" or a.get("name") != "lang":
                            self.assertTrue(a.get("aria-describedby"), "no explanation tied to the field")

    def test_personal_pages_ask_not_to_be_indexed(self):
        pages = every_page()
        for name in ("result", "result-manual", "settings", "delete", "deleted"):
            with self.subTest(page=name):
                self.assertIn('<meta name="robots" content="noindex">', pages[name])
        self.assertNotIn('name="robots"', pages["signup"])


class SignupPageTest(unittest.TestCase):
    def test_the_form_in_four_parts(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings(SUBSYNC_PUBLIC_INVITE="code")))
        self.assertEqual(len(tree.all("fieldset")), 4)
        self.assertEqual(len(tree.all("legend")), 4)
        for field in ("invite", "link", "password"):
            self.assertTrue(tree.all("input", name=field))
        self.assertTrue(tree.all("form", method="post", action="/signup"))
        self.assertEqual(len(Tree(pages.signup_page(settings())).all("fieldset")), 3)     # no invite part

    def test_languages_as_chips_english_first_by_default(self):
        from subsync import pages
        from subsync.aiosconfig import LANGUAGES
        tree = Tree(pages.signup_page(settings()))
        boxes = tree.all("input", type="checkbox", name="lang")
        self.assertEqual([a["value"] for a in boxes], [three for _, _, three in LANGUAGES])
        self.assertEqual([a["value"] for a in boxes if "checked" in a], ["eng"])
        self.assertEqual(tree.all("input", name="lang_order")[0]["value"], "eng")
        self.assertEqual(tree.all("input", name="lang_ui")[0]["value"], "1")
        self.assertTrue(tree.all("fieldset", **{"data-max": "4"}))

    def test_chosen_languages_come_back_in_order_after_an_error(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings(), error="x", order=["rus", "eng"]))
        self.assertEqual([a["value"] for a in tree.all("input", name="lang") if "checked" in a], ["eng", "rus"])
        self.assertEqual(tree.all("input", name="lang_order")[0]["value"], "rus,eng")

    def test_an_error_is_shown_at_its_field_and_in_a_summary(self):
        from subsync import pages
        text = pages.signup_page(settings(), error="Your AIOStreams didn't accept this password.", field="password")
        tree = Tree(text)
        box = tree.all("input", name="password")[0]
        self.assertEqual(box.get("aria-invalid"), "true")
        self.assertIn("password-error", box["aria-describedby"].split())
        self.assertIn("didn't accept", tree.text_of["password-error"])
        summary = tree.all("div", role="alert")
        self.assertEqual(len(summary), 1)
        self.assertTrue(tree.all("a", href="#password"))
        self.assertIn("aren't kept after an error", tree.text())

    def test_the_summary_links_only_to_a_field_the_page_has(self):
        from subsync import pages
        s = settings()
        tree = Tree(pages.settings_page(s, {"languages": ["eng"]}, {"files": 0, "bytes": 0}, LINK,
                                        error="AIOSubtitles can't reach your AIOStreams.", field="link"))
        self.assertFalse(tree.all("a", href="#link"))
        self.assertNotIn("aren't kept after an error", tree.text())
        tree = Tree(pages.signup_page(s, error="That invite code isn't right.", field="invite"))   # no invite field
        self.assertFalse(tree.all("a", href="#invite"))

    def test_the_languages_explanation_is_on_the_group_not_each_chip(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings()))
        self.assertEqual(tree.all("fieldset", **{"data-max": "4"})[0]["aria-describedby"], "languages-help")
        self.assertFalse([a for a in tree.all("input", name="lang") if a.get("aria-describedby")])

    def test_no_language_chosen_shows_none_ticked(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings(), error="Choose at least one language.", field="languages", order=[]))
        self.assertEqual([a for a in tree.all("input", name="lang") if "checked" in a], [])

    def test_an_error_without_a_field_is_in_the_summary_only(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings(), error="AIOSubtitles is full for now."))
        self.assertEqual(len(tree.all("div", role="alert")), 1)
        self.assertFalse([a for _, a in tree.elements if a.get("aria-invalid")])

    def test_before_you_start_names_the_attempt_limit_from_the_setting(self):
        from subsync import pages
        self.assertIn("7 sign-up attempts", Tree(pages.signup_page(settings(SUBSYNC_SIGNUPS_PER_HOUR="7"))).text())

    def test_the_illustration_is_described_and_hidden_parts_are_hidden(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings()))
        strip = tree.all("figure", role="img")
        self.assertEqual(len(strip), 1)
        self.assertTrue(strip[0].get("aria-label"))
        for _, a in tree.elements:
            if _ == "svg":
                self.assertEqual(a.get("aria-hidden"), "true")

    def test_buttons_the_script_adds_stay_hidden_without_it(self):
        from subsync import pages
        tree = Tree(pages.signup_page(settings()))
        reveal = tree.all("button", **{"data-reveal": "password"})
        self.assertEqual(len(reveal), 1)
        self.assertIn("hidden", reveal[0])
        self.assertEqual(reveal[0]["type"], "button")


class ResultPageTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_stand_in_has_the_real_results_fields(self):
        import dataclasses
        from subsync.signup import SignupResult
        self.assertEqual({f.name for f in dataclasses.fields(SignupResult)}, set(vars(result())))

    def test_link_once_settings_button_and_what_was_done(self):
        from subsync import pages
        text = pages.result_page(result())
        tree = Tree(text)
        self.assertEqual(text.count(LINK), 1)
        self.assertTrue(tree.all("a", href=f"{BASE}/u/{TOKEN}/configure"))
        self.assertIn("English, Russian", tree.text())
        self.assertIn("adds 1 subtitle source: AIOSubtitles", tree.text())
        self.assertIn("What happens next", tree.text())
        self.assertIn("Keep it private too", tree.text())
        copy = tree.all("button", **{"data-copy": "personal"})
        self.assertEqual(len(copy), 1)
        self.assertIn("hidden", copy[0])

    def test_manual_steps_and_the_real_debrid_warning(self):
        from subsync import pages
        text = pages.result_page(result(added=False, manual=True, changes=[], warning="Real-Debrid is left out: x."))
        tree = Tree(text)
        self.assertEqual(len(tree.all("ol")), 1)
        self.assertIn("One step left", tree.text())
        self.assertIn("Real-Debrid is left out", tree.text())
        self.assertNotIn("adds 1 subtitle source", tree.text())


class SettingsPageTest(unittest.TestCase):
    def page(self, usage=None, **kw):
        from subsync import pages
        return pages.settings_page(settings(), {"languages": ["rus", "eng"]}, usage or {"files": 3, "bytes": 0}, LINK,
                                   now=kw.pop("now", 0), **kw)

    def test_usage_and_the_reset_time(self):
        text = Tree(self.page(now=16 * 3600 + 48 * 60)).text()     # 16:48 UTC
        self.assertIn("3 of 40 new files", text)
        self.assertIn("Resets at 00:00 UTC, in 7 h 12 min", text)
        self.assertNotIn("limit is reached", text)

    def test_the_reset_time_reads_plainly(self):
        from subsync.pages import reset_in
        day = 86400
        for now, shown in ((0, "in 24 h"), (day - 30, "in 1 min"), (day - 600, "in 10 min"), (day - 3600, "in 1 h"),
                           (day - 3660, "in 1 h 1 min")):
            with self.subTest(now=now):
                self.assertEqual(reset_in(now), shown)

    def test_a_reached_limit_is_called_out(self):
        for usage in ({"files": 40, "bytes": 0}, {"files": 1, "bytes": 500 * 1024 * 1024}):
            with self.subTest(usage=usage):
                text = Tree(self.page(usage)).text()
                self.assertIn("Today's limit is reached", text)

    def test_success_warning_and_error_each_look_different(self):
        tree = Tree(self.page(message="Saved.", notice="Careful.", error="Wrong."))
        self.assertEqual(len(tree.all("div", role="status", **{"class": "note ok"})), 1)
        self.assertEqual(len(tree.all("div", **{"class": "note warn"})), 1)
        self.assertEqual(len(tree.all("div", role="alert", **{"class": "note error"})), 1)

    def test_languages_in_their_order(self):
        tree = Tree(self.page())
        self.assertEqual(sorted(a["value"] for a in tree.all("input", name="lang") if "checked" in a), ["eng", "rus"])
        self.assertEqual(tree.all("input", name="lang_order")[0]["value"], "rus,eng")
        self.assertNotIn("on your settings page", Tree(self.page()).text())

    def test_refresh_error_is_at_the_password(self):
        tree = Tree(self.page(error="Your AIOStreams didn't accept this password.", field="refresh"))
        self.assertEqual(tree.all("input", id="refresh")[0].get("aria-invalid"), "true")

    def test_done_messages_are_fixed_texts(self):
        from subsync import pages
        record = {"languages": ["rus", "eng"]}
        self.assertEqual(pages.done_message("languages", record)[0],
                         "Languages saved: Russian, English. They apply from the next file you play.")
        self.assertIn("now matches yours", pages.done_message("refreshed", record)[0])
        self.assertIn("Real-Debrid", pages.done_message("refreshed-rd", record)[1])
        message, notice = pages.done_message("refreshed-manual", record)
        self.assertIn("couldn't put itself back", notice)
        self.assertIn("paste your personal link as its manifest link", notice)
        self.assertIsNone(message)
        self.assertEqual(pages.done_message("<script>", record), (None, None))
        self.assertEqual(pages.done_message(None, record), (None, None))


class OtherPagesTest(unittest.TestCase):
    def test_delete_confirm_lists_what_goes_and_shows_an_error_at_the_password(self):
        from subsync import pages
        tree = Tree(pages.delete_confirm_page(error="Your AIOStreams didn't accept this password.", field="delete"))
        self.assertIn("can't be undone", tree.text())
        self.assertEqual(tree.all("input", id="delete")[0].get("aria-invalid"), "true")
        self.assertTrue(tree.all("input", type="hidden", name="action", value="delete"))
        self.assertTrue(tree.all("a", href="configure"))
        self.assertIn("Keep my data", tree.text())

    def test_deleted_page_says_what_is_left_to_do(self):
        from subsync import explain, pages
        done = Tree(pages.deleted_page({"sync_removed": True, "addon_removed": True})).text()
        self.assertNotIn("One step left", done)
        left = Tree(pages.deleted_page({"sync_removed": False, "addon_removed": False})).text()
        self.assertIn(explain.PUBLIC_TEXTS["sync_left"], left)
        self.assertIn(explain.PUBLIC_TEXTS["addon_left"], left)
        self.assertIn("Sign up again", done)

    def test_privacy_in_four_parts_with_the_same_words(self):
        from subsync import explain, pages
        self.assertEqual(" ".join(f"{lead} {text}".strip() for _, lead, text in explain.PRIVACY_PARTS),
                         explain.PUBLIC_TEXTS["privacy"])
        tree = Tree(pages.privacy_page(settings()))
        self.assertEqual(len(tree.all("h2")), len(explain.PRIVACY_PARTS) + 1)       # the parts and "Questions"
        self.assertTrue(tree.all("a", href=explain.CONTACT))

    def test_not_found_page(self):
        from subsync import pages
        text = Tree(pages.not_found_page()).text()
        self.assertIn("This page doesn't exist", text)
        self.assertIn("copy the whole link again", text)


class ScriptTest(unittest.TestCase):
    def test_the_script_is_small_local_and_respects_reduced_motion(self):
        from subsync.pages import APP_JS
        self.assertLess(len(APP_JS.encode()), 12 * 1024)
        self.assertIn("prefers-reduced-motion", APP_JS)
        self.assertNotRegex(APP_JS, r"https?://|\beval\(|new Function|innerHTML|outerHTML|insertAdjacentHTML|"
                                    r"document\.write|localStorage|sessionStorage|fetch\(|XMLHttpRequest|sendBeacon")
        self.assertNotRegex(APP_JS, r"\bAD\d{1,3}\b|S4-\d+")


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
                                       "of this config: the one you set on AIOStreams' configure page.")
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

    def test_headers_allow_only_our_own_script(self):
        from test_public_http import http
        code, _, headers = http(self.b.base + "/")
        self.assertEqual(headers["Content-Security-Policy"],
                         "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        self.assertNotIn("X-Robots-Tag", headers)
        code, body, headers = http(self.b.base + "/static/app.js")
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        from subsync.pages import APP_JS
        self.assertEqual(body.decode(), APP_JS)
        self.assertEqual(http(self.b.base + "/static/other.js")[0], 404)

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
        self.assertIn("didn't accept this password", tree.text_of["password-error"])
        self.assertEqual(tree.all("input", name="password")[0].get("aria-invalid"), "true")
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
