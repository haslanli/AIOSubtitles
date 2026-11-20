"""aiosconfig: the offered languages, the shapes of config IDs and links, and the AIOStreams client against stand-in
servers on this machine: every failure, of the network or of an answer's shape, comes back as an AiosError; answers are
read with a size cap and a time limit; a password is never sent on through a redirect."""
import gc
import json
import os
import time
import unittest
import types
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import HAVE_CRYPTOGRAPHY, NEEDS, TempDir, local_network_only, start_server, stop_server  # noqa: E402
from subsync import aiosconfig, net  # noqa: E402
from subsync.aiosconfig import BAD_ANSWER, AiosApi, AiosError  # noqa: E402

UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
CONFIG = {"services": [], "presets": []}


def no_sleep(waits):
    """aiosconfig's clock with its sleeps recorded in `waits` instead of slept (only aiosconfig's: the stand-in servers
    keep their own)."""
    return mock.patch.object(aiosconfig, "time", types.SimpleNamespace(sleep=waits.append, monotonic=time.monotonic))


# ------------------------------------------------------------------------------------------- the stand-in server

def send(h, code, body=b"", headers=None, length=None):
    h.send_response(code)
    for name, value in (headers or {}).items():
        h.send_header(name, value)
    h.send_header("Content-Length", str(len(body) if length is None else length))
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def answer(code, body, **headers):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return lambda h: send(h, code, raw, {"Content-Type": "application/json", **headers})


def cut_short(code, sent=b'{"succ', promised=100):
    """An answer whose body ends (the connection closed) before its Content-Length."""
    return lambda h: send(h, code, sent, {"Content-Type": "application/json"}, length=promised)


def drip(code, body, every):
    """An answer whose body comes one byte every `every` seconds."""
    def route(h):
        send(h, code, b"", {"Content-Type": "application/json"}, length=len(body))
        for i in range(len(body)):
            h.wfile.write(body[i:i + 1])
            h.wfile.flush()
            time.sleep(every)
    return route


def slow_head(code, body=b"", seconds=6, **headers):
    """An answer whose status line and headers come a byte at a time over `seconds` (each read's own wait is met),
    then its body at once."""
    def route(h):
        fields = {"Content-Type": "application/json", **headers, "Content-Length": str(len(body))}
        head = ("\r\n".join([f"HTTP/1.1 {code} X"] + [f"{k}: {v}" for k, v in fields.items()]) + "\r\n\r\n").encode()
        for i in range(len(head)):
            h.wfile.write(head[i:i + 1])
            h.wfile.flush()
            time.sleep(seconds / len(head))
        h.wfile.write(body)
    return route


def cut_status(code, seconds=3):
    """An answer whose status line never ends: its code, then a byte of its reason every 0.1 s for `seconds` (each
    read's own wait is met). The bound shuts it mid-line, and what came reads as a whole status line with no headers."""
    def route(h):
        h.wfile.write(f"HTTP/1.1 {code} ".encode())
        for _ in range(int(seconds * 10)):
            h.wfile.write(b"X")
            h.wfile.flush()
            time.sleep(0.1)
    return route


def big(code, size):
    """A JSON answer of `size` bytes, sent in pieces."""
    def route(h):
        send(h, code, b"", {"Content-Type": "application/json"}, length=size)
        h.wfile.write(b'{"x": "')
        left = size - len(b'{"x": "') - 2
        while left > 0:
            piece = min(left, 1 << 20)
            h.wfile.write(b"a" * piece)
            left -= piece
        h.wfile.write(b'"}')
    return route


def redirect(code, location):
    return lambda h: send(h, code, b"Moved", {"Location": location, "Content-Type": "text/plain"})


class Stand:
    """A server on this machine: `routes` maps (method, path) to a function writing the answer; every request is
    recorded as (method, path, whether it carried an Authorization header)."""

    def __init__(self):
        self.routes, self.requests = {}, []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle_any(self):
                path = self.path.partition("?")[0]
                outer.requests.append((self.command, path, bool(self.headers.get("Authorization"))))
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self.close_connection = True
                try:
                    outer.routes.get((self.command, path), answer(404, {"success": False}))(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the client stopped reading

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        stop_server(self.server)


class ClientCase(unittest.TestCase):
    def setUp(self):
        net.configure(public_only=False)
        self.stand = Stand()
        self.addCleanup(self.stand.close)
        guard = local_network_only()
        guard.start()
        self.addCleanup(guard.stop)

    def api(self, timeout=5, **kw):
        return AiosApi(self.stand.url, timeout=timeout, max_timeout=timeout, **kw)

    def calls(self, api):
        """Each config call with its stand-in route: (name, call, method)."""
        return (("get_config", lambda: api.get_config(UUID, "pw"), "GET"),
                ("save_config", lambda: api.save_config(UUID, "pw", CONFIG), "PUT"),
                ("create_config", lambda: api.create_config(CONFIG, "pw"), "POST"),
                ("delete_config", lambda: api.delete_config(UUID, "pw"), "DELETE"))


# ------------------------------------------------------------------------------------------- the offered languages

class LanguagesTest(unittest.TestCase):
    def test_the_offered_languages_are_the_32_the_service_always_offered(self):
        self.assertEqual(aiosconfig.LANGUAGES, [
            ('English', 'en', 'eng'), ('Spanish', 'es', 'spa'), ('French', 'fr', 'fre'), ('German', 'de', 'ger'),
            ('Italian', 'it', 'ita'), ('Portuguese', 'pt', 'por'), ('Russian', 'ru', 'rus'), ('Ukrainian', 'uk', 'ukr'),
            ('Polish', 'pl', 'pol'), ('Dutch', 'nl', 'dut'), ('Swedish', 'sv', 'swe'), ('Norwegian', 'no', 'nor'),
            ('Danish', 'da', 'dan'), ('Finnish', 'fi', 'fin'), ('Czech', 'cs', 'cze'), ('Hungarian', 'hu', 'hun'),
            ('Romanian', 'ro', 'rum'), ('Bulgarian', 'bg', 'bul'), ('Croatian', 'hr', 'hrv'), ('Serbian', 'sr', 'srp'),
            ('Greek', 'el', 'gre'), ('Turkish', 'tr', 'tur'), ('Arabic', 'ar', 'ara'), ('Hebrew', 'he', 'heb'),
            ('Persian', 'fa', 'per'), ('Hindi', 'hi', 'hin'), ('Indonesian', 'id', 'ind'), ('Thai', 'th', 'tha'),
            ('Vietnamese', 'vi', 'vie'), ('Japanese', 'ja', 'jpn'), ('Korean', 'ko', 'kor'), ('Chinese', 'zh', 'chi')])

    def test_names_codes_and_the_other_spellings_understood(self):
        understood = {"English": "eng", "ENGLISH": "eng", "en": "eng", "eng": "eng", "deu": "ger", "fra": "fre",
                      "nld": "dut", "ces": "cze", "ell": "gre", "ron": "rum", "zho": "chi", "fas": "per",
                      "farsi": "per", "Farsi": "per"}
        for typed, three in understood.items():
            with self.subTest(typed=typed):
                self.assertEqual(aiosconfig.parse_languages(typed)[0][0][2], three)
        for typed in ("pob", "pt-br", "en_US", "zh-Hans", "Klingon"):   # never understood
            with self.subTest(typed=typed):
                self.assertIn("Not a language I know", aiosconfig.parse_languages(typed)[1])
        self.assertEqual(len(aiosconfig._BY), 32 * 3 + 9)


# ------------------------------------------------------------------------------------------- IDs and links

class IdsAndLinksTest(unittest.TestCase):
    def test_a_config_id_is_found_in_text_and_must_be_whole_in_a_path(self):
        self.assertEqual(aiosconfig.config_id_from(f"https://a.test/stremio/{UUID.upper()}/x/manifest.json"), UUID)
        self.assertIsNone(aiosconfig.config_id_from("no id here"))
        api = AiosApi("https://aios.test")
        self.assertEqual(api._config_id_in(f"/stremio/{UUID.upper()}/ENC/manifest.json"), UUID)
        self.assertIsNone(api._config_id_in(f"/stremio/{UUID}0/ENC/manifest.json"))
        self.assertIsNone(api._config_id_in(f"/stremio/x{UUID}/ENC/manifest.json"))

    def test_install_link(self):
        self.assertEqual(aiosconfig.install_link("https://aios.test", UUID, "ENC"),
                         f"https://aios.test/stremio/{UUID}/ENC/manifest.json")

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_public_sign_up_and_routes_use_the_same_shapes(self):
        from subsync import public, signup, users
        self.assertIs(signup.WRONG_PASSWORD_CODES, aiosconfig.WRONG_PASSWORD_CODES)
        self.assertEqual(signup.parse_link(f"https://a.test/stremio/{UUID.upper()}/ENC/manifest.json")[1], UUID)
        for bad in (f"{UUID}0", UUID[:-1], f"x{UUID}"):
            with self.subTest(bad=bad), self.assertRaises(signup.SignupError):
                signup.parse_link(f"https://a.test/stremio/{bad}/ENC/manifest.json")
        token = users.new_token()
        self.assertTrue(users.TOKEN_RE.match(token))
        self.assertEqual(public.USER_PATH.match(f"/u/{token}/manifest.json").group(1), token)
        for bad in (token[:-1], token + "a", token[:-1] + "."):
            with self.subTest(bad=bad):
                self.assertIsNone(users.TOKEN_RE.match(bad))
                self.assertIsNone(public.USER_PATH.match(f"/u/{bad}/manifest.json"))


# ------------------------------------------------------------------------------------------- the client

class AnswerLimitsTest(ClientCase):
    """An answer is read with a size cap, and must be whole within the call's timeout."""

    def test_an_answer_larger_than_the_cap_is_refused(self):
        self.stand.routes[("GET", "/api/v1/status")] = big(200, aiosconfig.MAX_ANSWER + 1024)
        with self.assertRaises(AiosError) as e:
            self.api(timeout=20).call("GET", "/api/v1/status")
        self.assertEqual(e.exception.code, BAD_ANSWER)

    def test_an_answer_at_the_cap_is_read(self):
        self.stand.routes[("GET", "/api/v1/status")] = big(200, aiosconfig.MAX_ANSWER)
        status, body = self.api(timeout=20).call("GET", "/api/v1/status")
        self.assertEqual((status, len(body["x"])), (200, aiosconfig.MAX_ANSWER - 9))

    def test_a_dripping_answer_ends_at_the_timeout(self):
        body = b'{"success": true, "data": {"version": "x"}}'
        for code in (200, 500):
            with self.subTest(code=code):
                self.stand.routes[("GET", "/api/v1/status")] = drip(code, body, 0.2)
                t0 = time.monotonic()
                with self.assertRaises(AiosError) as e:
                    self.api(timeout=1).call("GET", "/api/v1/status")
                self.assertLess(time.monotonic() - t0, 3)
                self.assertIn("Can't reach", e.exception.message)
                self.assertIn("timed out", e.exception.message)

    def test_a_slow_but_steady_answer_within_the_timeout_is_read(self):
        self.stand.routes[("GET", "/api/v1/status")] = drip(200, b'{"a": 1}', 0.05)
        self.assertEqual(self.api(timeout=3).call("GET", "/api/v1/status"), (200, {"a": 1}))

    def test_an_alias_refusal_is_read_within_its_limits(self):
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = drip(
            400, b'{"error": {"code": "USER_INVALID_DETAILS"}}', 0.2)
        t0 = time.monotonic()
        with self.assertRaises(AiosError) as e:
            self.api(timeout=1).alias_config_id("fam")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(e.exception.code, "ALIAS_NO_CONFIG")   # its reason never came whole: no reason known
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = answer(
            400, {"error": {"code": "USER_INVALID_DETAILS"}})
        with self.assertRaises(AiosError) as e:
            self.api(timeout=1).alias_config_id("fam")
        self.assertEqual(e.exception.code, "ALIAS_UNKNOWN")


class WholeCallBoundTest(ClientCase):
    """A call's wait bounds the whole request, its status line, headers and redirects as well as its body; the
    body only was bounded, from its first read, so a server that sent the rest a byte now and then, each within a
    read's own wait, held a call (a sign-up's request, a job's place) as long as it liked."""

    VERSION = json.dumps({"success": True, "data": {"version": "2.37.0"}}).encode()

    def assert_timed_out(self, call, within=2.5):
        t0 = time.monotonic()
        with self.assertRaises(AiosError) as e:
            call()
        self.assertLess(time.monotonic() - t0, within)
        self.assertEqual(e.exception.code, aiosconfig.UNREACHABLE)
        self.assertIn("timed out", e.exception.message)

    def test_a_trickled_status_line_and_headers_end_at_the_timeout(self):
        self.stand.routes[("GET", "/api/v1/status")] = slow_head(200, self.VERSION)
        self.stand.routes[("GET", "/api/v1/user")] = slow_head(401, b'{"error": {"code": "USER_INVALID_PASSWORD"}}')
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = slow_head(404)
        for name, call in (("status", lambda api: api.status()), ("get_config", lambda api: api.get_config(UUID, "pw")),
                           ("alias", lambda api: api.alias_config_id("fam"))):
            with self.subTest(call=name):
                self.assert_timed_out(lambda: call(self.api(timeout=1)))

    def test_a_status_line_cut_at_the_bound_is_no_answer(self):
        # the bound shut the connection right after the status code: the 404 never came whole, so it is no "unknown
        # alias", and no 400 or 500 either; the call timed out
        for code in (404, 400, 500):
            self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = cut_status(code)
            with self.subTest(code=code):
                self.assert_timed_out(lambda: self.api(timeout=1).alias_config_id("fam"))
        self.stand.routes[("GET", "/api/v1/status")] = cut_status(404)
        self.assert_timed_out(lambda: self.api(timeout=1).status())

    def test_redirect_hops_share_the_calls_bound(self):
        # each hop comes within the call's second, the two together don't
        other = Stand()
        self.addCleanup(other.close)
        self.stand.routes[("GET", "/api/v1/status")] = slow_head(302, seconds=0.7,
                                                                 Location=f"{other.url}/api/v1/status")
        other.routes[("GET", "/api/v1/status")] = slow_head(200, self.VERSION, seconds=0.7)
        self.assertEqual(self.api(timeout=5).status(), {"version": "2.37.0"})
        self.assert_timed_out(lambda: self.api(timeout=1).status())

    def test_a_body_cut_at_the_bound_without_a_length_is_not_taken_for_the_answer(self):
        def endless(h):   # no Content-Length: the body ends when the connection does
            h.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"data\": {\"version\": \"x")
            h.wfile.flush()
            for _ in range(30):
                time.sleep(0.1)
                h.wfile.write(b"x")
                h.wfile.flush()
        self.stand.routes[("GET", "/api/v1/status")] = endless
        self.assert_timed_out(lambda: self.api(timeout=1).status())


class RedirectTest(ClientCase):
    """A call carrying a password never follows a redirect, so the password is never sent on."""

    def setUp(self):
        super().setUp()
        self.other = Stand()
        self.addCleanup(self.other.close)

    def test_a_password_is_never_sent_on_through_a_redirect(self):
        for code in (301, 302, 303, 307, 308):
            for name, call, method in self.calls(self.api()):
                if name == "create_config":   # its password is in the body, which urllib never sends on
                    continue
                with self.subTest(code=code, call=name):
                    self.stand.routes[(method, "/api/v1/user")] = redirect(code, f"{self.other.url}/api/v1/user")
                    with self.assertRaises(AiosError) as e:
                        call()
                    self.assertEqual(e.exception.code, BAD_ANSWER)
                    self.assertEqual(self.other.requests, [])

    def test_a_call_without_a_password_still_follows_a_redirect(self):
        self.stand.routes[("GET", "/api/v1/status")] = redirect(302, f"{self.other.url}/api/v1/status")
        self.other.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        self.assertEqual(self.api().status(), {"version": "2.37.0"})
        self.assertEqual(self.other.requests, [("GET", "/api/v1/status", False)])


class ClosedAnswersTest(ClientCase):
    """An error answer is closed once read, not left to the garbage collector."""

    def test_error_answers_are_closed(self):
        self.stand.routes[("GET", "/api/v1/user")] = answer(401, {"error": {"code": "USER_INVALID_PASSWORD"}})
        self.stand.routes[("GET", "/api/v1/status")] = answer(500, b"<html>down</html>")
        api = self.api()
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            for call in (lambda: api.get_config(UUID, "pw"), api.status):
                with self.assertRaises(AiosError):
                    call()
            gc.collect()
        self.assertEqual([str(w.message) for w in seen if "HTTPError" in str(w.message)], [])


class EveryFailureIsAnAiosErrorTest(ClientCase):
    """Answers cut short and answers of another shape come back as AiosError, never as IncompleteRead,
    KeyError, TypeError, AttributeError or ValueError."""

    SHAPES = {
        "a list": answer(200, [1]),
        "a JSON string refusal": answer(403, "Forbidden"),
        "a number refusal": answer(400, 5),
        "an error that is a list": answer(400, {"success": False, "error": [1]}),
        "an error message that is no text": answer(400, {"success": False, "error": {"message": ["x"]}}),
        "success without data": answer(200, {"success": True}),
        "data null": answer(200, {"success": True, "data": None}),
        "data a list": answer(200, {"success": True, "data": [1]}),
        "cut short": cut_short(200),
        "a refusal cut short": cut_short(500),
    }

    def test_config_calls(self):
        api = self.api()
        succeeded = ("success without data", "data null", "data a list")   # all a save or a removal needs
        for shape, route in self.SHAPES.items():
            for name, call, method in self.calls(api):
                with self.subTest(shape=shape, call=name):
                    self.stand.routes[(method, "/api/v1/user")] = route
                    if shape in succeeded and name in ("save_config", "delete_config"):
                        call()
                        continue
                    with self.assertRaises(AiosError):   # anything else fails the test
                        call()

    def test_the_wrong_shapes_of_a_config_read(self):
        api = self.api()
        for data in ({"userData": "not a config", "encryptedPassword": "E"}, {"userData": {}},
                     {"userData": {}, "encryptedPassword": 5}):
            with self.subTest(data=data):
                self.stand.routes[("GET", "/api/v1/user")] = answer(200, {"success": True, "data": data})
                with self.assertRaises(AiosError) as e:
                    api.get_config(UUID, "pw")
                self.assertEqual(e.exception.code, BAD_ANSWER)
        self.stand.routes[("POST", "/api/v1/user")] = answer(201, {"success": True, "data": {"uuid": 5}})
        with self.assertRaises(AiosError) as e:
            api.create_config(CONFIG, "pw")
        self.assertEqual(e.exception.code, BAD_ANSWER)

    def test_answers_of_aiostreams_shape_still_work(self):
        api = self.api()
        self.stand.routes[("GET", "/api/v1/user")] = answer(200, {"success": True, "data": {
            "userData": CONFIG, "encryptedPassword": "E"}})
        self.stand.routes[("PUT", "/api/v1/user")] = answer(200, {"success": True})
        self.stand.routes[("POST", "/api/v1/user")] = answer(201, {"success": True, "data": {
            "uuid": UUID, "encryptedPassword": "E"}})
        self.stand.routes[("DELETE", "/api/v1/user")] = answer(200, {"success": True})
        self.assertEqual(api.get_config(UUID, "pw"), (CONFIG, "E"))
        self.assertIsNone(api.save_config(UUID, "pw", CONFIG))
        self.assertEqual(api.create_config(CONFIG, "pw"), (UUID, "E"))
        self.assertIsNone(api.delete_config(UUID, "pw"))
        for body, words in (({"success": False, "error": {"code": "USER_INVALID_PASSWORD", "message": "no"}},
                             "Reading the config: That config ID and password don't match"),
                            ({"success": False, "detail": "a detail"}, "Reading the config: a detail"),
                            ([], "Reading the config: AIOStreams gave an error without a message."),
                            (b"<html>Bad gateway</html>", "Reading the config: <html>Bad gateway</html>")):
            with self.subTest(body=body):
                self.stand.routes[("GET", "/api/v1/user")] = answer(502, body)
                with self.assertRaises(AiosError) as e:
                    api.get_config(UUID, "pw")
                self.assertTrue(e.exception.message.startswith(words), e.exception.message)
                self.assertNotEqual(e.exception.code, BAD_ANSWER)

    def test_other_calls(self):
        api = self.api()
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": 5})
        with self.assertRaises(AiosError):
            api.status()
        self.stand.routes[("POST", "/api/v1/auth/login")] = answer(200, [1])
        with self.assertRaises(AiosError):
            api.sign_in("admin", "pw")
        self.stand.routes[("POST", "/api/v1/auth/login")] = answer(200, {"success": True, "data": {"isAdmin": True}})
        self.assertTrue(api.sign_in("admin", "pw"))
        self.stand.routes[("GET", "/api/v1/dashboard/settings")] = answer(200, {"data": {"settings": ["x", 5]}})
        self.assertIsNone(api.subtitle_cache_setting())
        self.stand.routes[("GET", "/api/v1/dashboard/settings")] = answer(200, {"data": [1]})
        with self.assertRaises(AiosError):
            api.subtitle_cache_setting()
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = cut_short(400)
        with self.assertRaises(AiosError):
            api.alias_config_id("fam")

    def test_an_address_with_a_bad_port(self):
        api = AiosApi("http://127.0.0.1:abc", timeout=2)
        for call in (api.status, lambda: api.alias_config_id("fam"), lambda: api.get_config(UUID, "pw")):
            with self.assertRaises(AiosError) as e:
                call()
            self.assertIn("Can't reach", e.exception.message)

    def test_an_address_without_a_scheme(self):
        # Urllib refuses it (ValueError) as the request is made, which was outside the client's try
        api = AiosApi("aios.example", timeout=2)
        for call in (api.status, lambda: api.alias_config_id("fam"), lambda: api.get_config(UUID, "pw")):
            with self.assertRaises(AiosError) as e:
                call()
            self.assertEqual(e.exception.code, aiosconfig.UNREACHABLE)
            self.assertIn("Can't reach AIOStreams at aios.example", e.exception.message)

    def test_an_answer_nested_too_deep_to_decode(self):
        # json.loads raises RecursionError, not ValueError, for JSON nested deeper than it decodes
        deep = b"[" * 200_000
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, deep)
        with self.assertRaises(AiosError) as e:
            self.api().status()
        self.assertEqual(e.exception.code, aiosconfig.UNREADABLE)
        for name, call, method in self.calls(self.api()):
            with self.subTest(call=name):
                self.stand.routes[(method, "/api/v1/user")] = answer(400, deep)
                with self.assertRaises(AiosError):
                    call()
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = answer(400, deep)
        with self.assertRaises(AiosError) as e:
            self.api().alias_config_id("fam")
        self.assertEqual(e.exception.code, aiosconfig.ALIAS_NO_CONFIG)
        self.assertIsNone(aiosconfig._error_code(deep))

    def test_an_addon_route_of_another_shape(self):
        # An addon route's answer is a JSON object ({} for one that isn't JSON); a list was handed back as it
        # was, and ./setup's .get("streams") on it raised AttributeError
        base, path = f"{self.stand.url}/stremio/{UUID}/E", "/stream/movie/tt0816692.json"
        route = ("GET", f"/stremio/{UUID}/E/stream/movie/tt0816692.json")
        self.stand.routes[route] = answer(200, [{"name": "a row"}])
        with self.assertRaises(AiosError) as e:
            self.api().addon_get(base, path)
        self.assertEqual(e.exception.code, BAD_ANSWER)
        for body, read in (({"streams": [{"name": "a row"}]}, {"streams": [{"name": "a row"}]}),
                           (b"<html>ok</html>", {})):
            with self.subTest(body=body):
                self.stand.routes[route] = answer(200, body)
                self.assertEqual(self.api().addon_get(base, path), read)
        self.stand.routes[route] = answer(502, b"<html>down</html>")
        with self.assertRaises(AiosError) as e:
            self.api().addon_get(base, path)
        self.assertEqual((e.exception.code, e.exception.message), (502, "AIOStreams answered HTTP 502 for stream."))

    def test_a_retry_after_that_is_no_ascii_number(self):
        self.stand.routes[("GET", "/api/v1/status")] = answer(429, {}, **{"Retry-After": "²"})
        waits = []
        with no_sleep(waits):
            status, _ = self.api(on_wait=lambda s: None).call("GET", "/api/v1/status")
        self.assertEqual((status, waits), (429, [aiosconfig.SLOW_DOWN_WAIT] * (aiosconfig.TRIES - 1)))


    def test_a_retry_after_longer_than_int_reads_is_no_wait(self):
        # Int refuses more than 4,300 digits: such a Retry-After made a call's 429 "can't reach AIOStreams"
        # and raised ValueError out of an alias lookup
        long = "1" * 5000
        self.stand.routes[("GET", "/api/v1/status")] = answer(429, {}, **{"Retry-After": long})
        waits = []
        with no_sleep(waits):
            status, _ = self.api(on_wait=lambda s: None).call("GET", "/api/v1/status")
        self.assertEqual((status, waits), (429, [aiosconfig.SLOW_DOWN_WAIT] * (aiosconfig.TRIES - 1)))
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = answer(429, {}, **{"Retry-After": long})
        with no_sleep([]), self.assertRaises(AiosError) as e:
            self.api(on_wait=lambda s: None).alias_config_id("fam")
        self.assertEqual((e.exception.code, e.exception.message), ("RATE_LIMIT_EXCEEDED", aiosconfig.LIMITED))


class ErrorKindsTest(ClientCase):
    """Aiosconfig's own errors carry codes of their own and AIOStreams' keep its codes; what a refusal means
    (AiosError.kind) is read once, from AIOStreams' code and its own words, so the message says exactly that."""

    def test_aiosconfigs_own_errors_have_codes_and_aiostreams_codes_are_kept(self):
        with self.assertRaises(AiosError) as e:
            AiosApi("http://127.0.0.1:9", timeout=2).status()
        self.assertEqual(e.exception.code, aiosconfig.UNREACHABLE)
        self.assertTrue(e.exception.message.startswith("Can't reach AIOStreams at http://127.0.0.1:9 ("))
        self.stand.routes[("GET", "/api/v1/status")] = answer(404, {"nothing": "here"})
        with self.assertRaises(AiosError) as e:
            self.api().status()
        self.assertEqual((e.exception.code, e.exception.message),
                         (aiosconfig.NOT_AIOSTREAMS, f"{self.stand.url} answered, but not like AIOStreams (HTTP 404)."))
        for code in ("USER_INVALID_CONFIG", "INTERNAL", "RATE_LIMIT_EXCEEDED"):
            with self.subTest(code=code):
                self.stand.routes[("GET", "/api/v1/user")] = answer(400, {"error": {"code": code, "message": "x"}})
                with no_sleep([]), self.assertRaises(AiosError) as e:
                    self.api().get_config(UUID, "pw")
                self.assertEqual(e.exception.code, code)

    def test_the_kind_is_what_the_message_says(self):
        # a sign-in or a limit is told by AIOStreams' code or its own words, the error's own (its message) or the
        # answer's detail beside it: the message says so exactly when the kind does
        for body, kind, words in (
                ({"error": {"code": "FORBIDDEN"}}, aiosconfig.SIGN_IN, "asks you to sign in"),
                ({"error": {"message": "Not allowed without an account"}}, aiosconfig.SIGN_IN, "asks you to sign in"),
                ({"detail": "Not allowed"}, aiosconfig.SIGN_IN, "asks you to sign in"),
                ({"error": {"code": "X", "message": "Too many configs"}}, aiosconfig.LIMITING, "is limiting requests"),
                ({"detail": "Too many requests"}, aiosconfig.LIMITING, "is limiting requests"),
                ({"error": {"code": "USER_INVALID_PASSWORD", "message": "not allowed"}}, aiosconfig.NO_CONFIG,
                 "don't match a config"),
                ({"error": {"code": "USER_INVALID_CONFIG", "message": "too many presets"}}, aiosconfig.CONFIG_REFUSED,
                 "refused the config: too many presets"),
                ({"error": {"code": "X", "message": "please sign in first"}}, None, "please sign in first")):
            with self.subTest(body=body):
                self.stand.routes[("PUT", "/api/v1/user")] = answer(400, body)
                with self.assertRaises(AiosError) as e:
                    self.api().save_config(UUID, "pw", CONFIG)
                self.assertEqual(e.exception.kind, kind)
                self.assertIn(words, e.exception.message)
                self.assertIs(aiosconfig.needs_sign_in(e.exception), kind == aiosconfig.SIGN_IN)
                self.assertIs(aiosconfig.is_limited(e.exception), kind == aiosconfig.LIMITING)

    def test_a_too_many_requests_answer_is_a_limit_whatever_its_code(self):
        # The HTTP status says it; a 429 whose JSON names another code, in words without "too many", was read
        # as no kind at all
        self.stand.routes[("PUT", "/api/v1/user")] = answer(
            429, {"error": {"code": "TOO_MANY_REQUESTS", "message": "Slow down"}}, **{"Retry-After": "3600"})
        with no_sleep([]), self.assertRaises(AiosError) as e:
            self.api().save_config(UUID, "pw", CONFIG)
        self.assertEqual((e.exception.code, e.exception.kind, e.exception.message),
                         ("TOO_MANY_REQUESTS", aiosconfig.LIMITING, f"Saving the config: {aiosconfig.LIMITED}"))
        self.assertTrue(aiosconfig.is_limited(e.exception))

    def test_a_request_to_real_debrid_has_a_code_of_its_own(self):
        # It was the one error aiosconfig makes itself without a code; stopped before any connection
        api = AiosApi("https://real-debrid.com", timeout=2)
        for call in (api.status, lambda: api.alias_config_id("fam"), lambda: api.get_config(UUID, "pw")):
            with self.assertLogs("subsync.realdebrid", "WARNING"), self.assertRaises(AiosError) as e:
                call()
            self.assertEqual(e.exception.code, aiosconfig.REAL_DEBRID)
            self.assertIsNone(e.exception.kind)


GARBLED = {"cut short": b'{"success": true, "data": {"vers', "not UTF-8": b'{"success": true, "x": "\xff"}'}


class SetupWordsTest(ClientCase):
    """What ./setup says when AIOStreams answers oddly: a garbled answer is not "can't reach", the hourly
    limit only for making a config, an alias lookup's rate limit and server errors by name."""

    def test_a_garbled_answer_is_unreadable_not_unreachable(self):
        for shape, raw in GARBLED.items():
            with self.subTest(shape=shape):
                self.stand.routes[("GET", "/api/v1/status")] = answer(200, raw)
                with self.assertRaises(AiosError) as e:
                    self.api().status()
                self.assertEqual(e.exception.code, aiosconfig.UNREADABLE)
                # as ./setup shows it (the message, then its fix)
                self.assertEqual(f"{e.exception.message} {e.exception.fix}",
                                 f"{self.stand.url} answered, but its answer isn't the JSON AIOStreams sends (it may "
                                 "have been cut short). Try again; if it keeps happening, check the web server or "
                                 "proxy in front of AIOStreams.")

    def test_the_hourly_limit_is_named_only_when_making_a_config(self):
        limited = answer(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}}, **{"Retry-After": "3600"})
        api = self.api()
        for name, call, method in self.calls(api):
            with self.subTest(call=name):
                self.stand.routes[(method, "/api/v1/user")] = limited
                with self.assertRaises(AiosError) as e:
                    call()
                step = e.exception.message.partition(": ")[0]
                if name == "create_config":
                    self.assertEqual(e.exception.message, "Creating the sync config: AIOStreams is limiting requests "
                                                          "from this machine (it allows 10 new configs an hour); try "
                                                          "again later.")
                else:
                    self.assertEqual(e.exception.message, f"{step}: AIOStreams is limiting requests from this machine; "
                                                          "try again in a few minutes.")
                self.assertEqual(e.exception.reason, e.exception.message.partition(": ")[2])   # without the step

    def test_an_alias_lookup_waits_out_a_short_limit_once(self):
        tries = []

        def limited_once(h):
            tries.append(1)
            if len(tries) == 1:
                return send(h, 429, b'{"error": {"code": "RATE_LIMIT_EXCEEDED"}}', {"Retry-After": "2"})
            return redirect(302, f"/stremio/{UUID}/ENC/manifest.json")(h)
        self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = limited_once
        waits, said = [], []
        with no_sleep(waits):
            self.assertEqual(self.api(on_wait=said.append).alias_config_id("fam"), UUID)
        self.assertEqual((waits, said, len(tries)), ([2], [2], 2))

    def test_an_alias_lookup_on_a_busy_or_failing_aiostreams_names_it(self):
        limited = "AIOStreams is limiting requests from this machine; try again in a few minutes."
        for status, wait, asked, words in (
                (429, "2", 2, limited),              # still limited after the wait: asked once more only
                (429, "3600", 1, limited),           # a long limit: reported at once
                (500, None, 1, "AIOStreams answered HTTP 500 for the alias link; try again in a few minutes."),
                (502, None, 1, "AIOStreams answered HTTP 502 for the alias link; try again in a few minutes."),
                (503, None, 1, "AIOStreams answered HTTP 503 for the alias link; try again in a few minutes.")):
            with self.subTest(status=status, wait=wait):
                self.stand.requests.clear()
                self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = answer(
                    status, {"error": {"code": "X"}}, **({"Retry-After": wait} if wait else {}))
                waits = []
                with no_sleep(waits), self.assertRaises(AiosError) as e:
                    self.api(on_wait=lambda s: None).alias_config_id("fam")
                self.assertEqual(e.exception.message, words)               # never "no config for that alias"
                self.assertEqual(e.exception.code, "RATE_LIMIT_EXCEEDED" if status == 429 else status)
                self.assertEqual(len(self.stand.requests), asked)
                self.assertEqual(waits, [2] if asked == 2 else [])


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class SignupCallersTest(ClientCase):
    """The public sign-up's steps meet the client's AiosError only, and the public client waits out a short
    "too many requests" that names no wait."""

    def setUp(self):
        super().setUp()
        from subsync import signup
        self.signup = signup

    def public(self):
        return self.signup.public_api(self.stand.url, 5)

    def test_a_config_made_is_removed_when_its_manifest_answer_is_cut_short(self):
        self.stand.routes[("POST", "/api/v1/user")] = answer(201, {"success": True, "data": {
            "uuid": UUID, "encryptedPassword": "E"}})
        self.stand.routes[("GET", f"/stremio/{UUID}/E/manifest.json")] = cut_short(200)
        self.stand.routes[("DELETE", "/api/v1/user")] = answer(200, {"success": True})
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup._new_sync(self.public(), self.stand.url, CONFIG)
        self.assertIn("Checking AIOSubtitles' config on your AIOStreams", e.exception.message)
        self.assertIn(("DELETE", "/api/v1/user", True), self.stand.requests)

    def test_a_made_config_of_another_shape(self):
        for body in ([1], {"success": True, "data": [1]}, {"success": True}):
            with self.subTest(body=body):
                self.stand.routes[("POST", "/api/v1/user")] = answer(201, body)
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._new_sync(self.public(), self.stand.url, CONFIG)
                self.assertEqual(e.exception.message,
                                 "Your AIOStreams answered in a way AIOSubtitles doesn't know when making its config.")

    def test_reading_a_config_of_another_shape(self):
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        for data in ({"success": True}, [1], {"success": True, "data": {"userData": "x", "encryptedPassword": "E"}}):
            with self.subTest(data=data):
                self.stand.routes[("GET", "/api/v1/user")] = answer(200, data)
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._read_viewer(self.public(), UUID, "pw")
                self.assertEqual(e.exception.message, "Your AIOStreams answered in a way AIOSubtitles doesn't know. "
                                                      "Is the link from AIOStreams?")
                self.assertIsNone(e.exception.field)

    def test_delete_with_a_config_read_of_another_shape_deletes_nothing(self):
        from subsync.settings import Settings
        from subsync.users import Users, new_token
        with TempDir() as d:
            s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://subs.test",
                          "SUBSYNC_SECRET_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"})
            users = Users(os.path.join(d, "users.sqlite3"), s.secret_bytes())
            try:
                token = new_token()
                users.create(token, {"instance": self.stand.url, "main_uuid": UUID, "languages": ["eng"]})
                self.stand.routes[("GET", "/api/v1/user")] = answer(200, {"success": True})
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup.delete(s, users, token, {"password": "pw"}, api_factory=lambda base, t: self.public())
                self.assertEqual(e.exception.message,
                                 "Your AIOStreams answered in a way AIOSubtitles doesn't know. Nothing was deleted.")
                self.assertIsNotNone(users.open(token))
                self.assertEqual([r for r in self.stand.requests if r[0] == "DELETE"], [])
            finally:
                users.close()

    def test_a_short_limit_without_a_named_wait_is_waited_out(self):
        tries = []

        def limited_once(h):
            tries.append(1)
            if len(tries) == 1:
                return send(h, 429, b'{"error": {"code": "RATE_LIMIT_EXCEEDED"}}')
            return answer(200, {"success": True, "data": {"version": "2.37.0"}})(h)
        self.stand.routes[("GET", "/api/v1/status")] = limited_once
        waits = []
        with no_sleep(waits):
            self.assertEqual(self.public().status(), {"version": "2.37.0"})
        self.assertEqual(waits, [aiosconfig.PUBLIC_RETRY_WAIT_CAP])
        self.assertEqual(len(tries), 2)

    def test_a_long_limit_is_still_reported_at_once(self):
        self.stand.routes[("POST", "/api/v1/user")] = answer(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}},
                                                             **{"Retry-After": "3600"})
        waits = []
        with no_sleep(waits):
            with self.assertRaises(self.signup.SignupError) as e:
                self.signup._new_sync(self.public(), self.stand.url, CONFIG)
        self.assertIn("Try again in an hour", e.exception.message)
        self.assertEqual(waits, [])

    # -- the public pages' words for AIOStreams' errors: one step name, AIOStreams' own words quoted ------
    def record(self):
        return {"instance": self.stand.url, "sync_uuid": UUID, "sync_link": "L", "sync_password": "pw"}

    def test_an_error_names_the_pages_step_only_and_quotes_aiostreams(self):
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        self.stand.routes[("GET", "/api/v1/user")] = answer(500, {"success": False, "error": {
            "code": "INTERNAL", "message": "Internal error"}})
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup._read_viewer(self.public(), UUID, "pw")
        self.assertEqual(e.exception.message, 'Reading your AIOStreams config: your AIOStreams answered "Internal '
                                              'error".')
        self.stand.routes[("PUT", "/api/v1/user")] = answer(500, {"success": False, "error": {
            "code": "INTERNAL", "message": "boom"}})
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup._put_sync(self.public(), self.record(), CONFIG)
        self.assertEqual(e.exception.message, 'Updating AIOSubtitles\' config on your AIOStreams: your AIOStreams '
                                              'answered "boom".')

    def test_a_refusal_to_make_configs_is_said_for_the_person_signing_up(self):
        for body in ({"success": False, "error": {"code": "FORBIDDEN", "message": "Forbidden"}},
                     {"success": False, "error": {"code": "X", "message": "Creating configs is not allowed"}}):
            with self.subTest(body=body):
                self.stand.routes[("POST", "/api/v1/user")] = answer(403, body)
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._new_sync(self.public(), self.stand.url, CONFIG)
                self.assertEqual(e.exception.message, "Making AIOSubtitles' config on your AIOStreams: your "
                                                      "AIOStreams doesn't let AIOSubtitles make or save configs "
                                                      "there. Ask whoever runs your AIOStreams.")

    def test_a_refused_config_quotes_aiostreams_reason(self):
        self.stand.routes[("PUT", "/api/v1/user")] = answer(400, {"success": False, "error": {
            "code": "USER_INVALID_CONFIG", "message": "bad config"}})
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup._put_sync(self.public(), self.record(), CONFIG)
        self.assertEqual(e.exception.message, 'Updating AIOSubtitles\' config on your AIOStreams: your AIOStreams '
                                              'refused the config: "bad config".')
        self.assertEqual(e.exception.refused, "bad config")

    def test_the_hourly_limit_is_said_only_when_making_a_config(self):
        # AIOStreams limits new configs per hour; any other call for seconds
        limited = answer(429, {"error": {"code": "RATE_LIMIT_EXCEEDED"}}, **{"Retry-After": "3600"})
        self.stand.routes[("POST", "/api/v1/user")] = limited
        with no_sleep([]), self.assertRaises(self.signup.SignupError) as e:
            self.signup._new_sync(self.public(), self.stand.url, CONFIG)
        self.assertEqual(e.exception.message, "Making AIOSubtitles' config on your AIOStreams: your AIOStreams limits "
                                              "how many configs one address can make in an hour, and AIOSubtitles has "
                                              "made many there lately. Try again in an hour.")
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        self.stand.routes[("GET", "/api/v1/user")] = limited
        self.stand.routes[("PUT", "/api/v1/user")] = limited
        for step, call in (("Reading your AIOStreams config",
                            lambda: self.signup._read_viewer(self.public(), UUID, "pw")),
                           ("Updating AIOSubtitles' config on your AIOStreams",
                            lambda: self.signup._put_sync(self.public(), self.record(), CONFIG))):
            with self.subTest(step=step):
                with no_sleep([]), self.assertRaises(self.signup.SignupError) as e:
                    call()
                self.assertEqual(e.exception.message, f"{step}: your AIOStreams is limiting requests from "
                                                      "AIOSubtitles right now. Try again in a few minutes.")

    def test_a_too_many_requests_answer_is_said_as_a_limit_whatever_its_code(self):
        # The page told a limit by AIOStreams' own code or a bare 429 only; another code quoted its words
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        self.stand.routes[("GET", "/api/v1/user")] = answer(
            429, {"error": {"code": "TOO_MANY_REQUESTS", "message": "Slow down"}}, **{"Retry-After": "3600"})
        with no_sleep([]), self.assertRaises(self.signup.SignupError) as e:
            self.signup._read_viewer(self.public(), UUID, "pw")
        self.assertEqual(e.exception.message, "Reading your AIOStreams config: your AIOStreams is limiting requests "
                                              "from AIOSubtitles right now. Try again in a few minutes.")

    def test_a_garbled_answer_is_not_said_to_be_unreachable(self):
        # It answered, so the person's network isn't the problem, and no field is marked
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        for shape, raw in GARBLED.items():
            with self.subTest(shape=shape):
                self.stand.routes[("GET", "/api/v1/user")] = answer(200, raw)
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._read_viewer(self.public(), UUID, "pw")
                self.assertEqual(e.exception.message, "Reading your AIOStreams config: your AIOStreams answered in a "
                                                      "way AIOSubtitles doesn't know. Try again in a few minutes.")
                self.assertIsNone(e.exception.field)

    def test_aiostreams_own_words_are_never_taken_for_another_kind_of_error(self):
        # Signup told an unreachable AIOStreams, an answer that isn't AIOStreams' and a limit by words in the
        # error's message, which quotes AIOStreams' own words for a refused config or an error it names; such a refusal
        # was said to be "can't reach your AIOStreams" or "limiting requests", and marked the link
        self.stand.routes[("GET", "/api/v1/status")] = answer(200, {"success": True, "data": {"version": "2.37.0"}})
        for words in ("Can't reach TorBox", "Its answer was not like AIOStreams' own", "limiting requests to TorBox"):
            with self.subTest(words=words):
                self.stand.routes[("PUT", "/api/v1/user")] = answer(400, {"success": False, "error": {
                    "code": "USER_INVALID_CONFIG", "message": words}})
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._put_sync(self.public(), self.record(), CONFIG)
                self.assertEqual((e.exception.message, e.exception.refused, e.exception.field),
                                 (f'Updating AIOSubtitles\' config on your AIOStreams: your AIOStreams refused the '
                                  f'config: "{words}".', words, None))
                self.stand.routes[("GET", "/api/v1/user")] = answer(500, {"success": False, "error": {
                    "code": "INTERNAL", "message": words}})
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._read_viewer(self.public(), UUID, "pw")
                self.assertEqual((e.exception.message, e.exception.field),
                                 (f'Reading your AIOStreams config: your AIOStreams answered "{words}".', None))

    def test_an_unreachable_aiostreams_and_one_that_isnt_aiostreams_are_still_told_apart(self):
        # The same words and the same field as before, now told by the error's code
        dead = self.signup.public_api("http://127.0.0.1:9", 5)
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup._read_viewer(dead, UUID, "pw")
        self.assertEqual((e.exception.message, e.exception.field),
                         ("Reading your AIOStreams config: AIOSubtitles can't reach your AIOStreams from the internet. "
                          "Check the link, and that your AIOStreams can be opened from outside your home network.",
                          "link"))
        self.stand.routes[("GET", "/api/v1/status")] = answer(404, {"nothing": "here"})
        with self.assertRaises(self.signup.SignupError) as e:
            self.signup._read_viewer(self.public(), UUID, "pw")
        self.assertEqual((e.exception.message, e.exception.field),
                         (f"Reading your AIOStreams config: {self.stand.url} answered, but not like AIOStreams (HTTP "
                          "404).", "link"))
        for body in ({"success": False, "error": {"code": "X", "message": "Too many configs"}},
                     {"success": False, "detail": "Too many requests"}):
            with self.subTest(body=body):
                self.stand.routes[("PUT", "/api/v1/user")] = answer(400, body)
                with self.assertRaises(self.signup.SignupError) as e:
                    self.signup._put_sync(self.public(), self.record(), CONFIG)
                self.assertEqual(e.exception.message,
                                 "Updating AIOSubtitles' config on your AIOStreams: your AIOStreams is limiting "
                                 "requests from AIOSubtitles right now. Try again in a few minutes.")

    def test_an_alias_link_on_a_busy_or_failing_aiostreams(self):
        # The link is fine; the page names what its AIOStreams did
        for status, words in ((429, "Opening your alias link: your AIOStreams is limiting requests from AIOSubtitles "
                                    "right now. Try again in a few minutes."),
                              (502, "Opening your alias link: your AIOStreams answered with an error (HTTP 502). Try "
                                    "again in a few minutes.")):
            with self.subTest(status=status):
                self.stand.routes[("GET", "/stremio/u/fam/manifest.json")] = answer(
                    status, {"error": {"code": "X"}}, **{"Retry-After": "3600"})
                with no_sleep([]), self.assertRaises(self.signup.SignupError) as e:
                    self.signup._config_id(self.public(), None, "fam")
                self.assertEqual(e.exception.message, words)


if __name__ == "__main__":
    unittest.main()
