"""Real-Debrid is never used: the config subsync lists through, each listed row, and every web request (local fakes
only; nothing here reaches a debrid service)."""
import base64
import json
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import local_network_only, start_server, stop_server  # noqa: E402
from subsync import realdebrid  # noqa: E402
from subsync.reader import RangeReader, ReadFailed  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.sources import Aios, fetch_subtitle, unreadable  # noqa: E402

TORBOX = {'id': 'torbox', 'enabled': True, 'credentials': {'apiKey': 'tb'}}
REAL_DEBRID = {'id': 'realdebrid', 'enabled': True, 'credentials': {'apiKey': 'rd'}}
NZBDAV = {'id': 'nzbdav', 'enabled': True, 'credentials': {}}


def link_addon(link, name='Linked', resources=None):
    options = {'name': name, 'manifestUrl': link}
    if resources is not None:
        options['resources'] = resources
    return {'type': 'custom', 'instanceId': name[:3].lower(), 'enabled': True, 'options': options}


def encoded(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')


class ServicesNamedTest(unittest.TestCase):
    def test_a_plain_link_names_its_service(self):
        self.assertEqual(realdebrid.services_named('https://torrentio.example/providers=yts|realdebrid=KEY/manifest.json'),
                         {'realdebrid'})
        self.assertEqual(realdebrid.services_named('https://torrentio.example/torbox=KEY/manifest.json'), {'torbox'})

    def test_an_encoded_link_names_its_service(self):
        comet = f'https://comet.example/{encoded({"debridService": "realdebrid", "debridApiKey": "x"})}/manifest.json'
        self.assertEqual(realdebrid.services_named(comet), {'realdebrid'})
        stremthru = f'https://stremthru.example/stremio/torz/{encoded({"stores": [{"c": "rd", "t": "x"}]})}/manifest.json'
        self.assertEqual(realdebrid.services_named(stremthru), {'realdebrid'})
        jackettio = f'https://jackettio.example/{encoded({"debridId": "torbox"})}/manifest.json'
        self.assertEqual(realdebrid.services_named(jackettio), {'torbox'})

    def test_standard_base64_of_accented_or_non_latin_settings_is_read_whole(self):
        # Standard base64 writes "/" too; cut there before it was decoded, settings that hold accented or
        # non-Latin text were read wrong from that "/" on, and a Real-Debrid name after it was missed
        for name in ('xxZoë', 'пример'):
            settings = json.dumps({'debridService': 'torbox', 'name': name, 'fallbackService': 'realdebrid'},
                                  ensure_ascii=False)
            standard = base64.b64encode(settings.encode()).decode()
            self.assertIn('/', standard)
            for link in (f'https://addon.example/manifest.json?config={standard}',
                         f'https://addon.example/{standard.replace("/", "%2F")}/manifest.json'):
                with self.subTest(link=link):
                    self.assertEqual(realdebrid.services_named(link), {'torbox', 'realdebrid'})
                    new, changes = realdebrid.without_real_debrid({'services': [TORBOX],
                                                                   'presets': [link_addon(link, 'Accented')]})
                    self.assertEqual((new['presets'], changes), ([], ['"Accented" left out: its link names Real-Debrid']))

    def test_an_unreadable_link_names_nothing(self):
        self.assertEqual(realdebrid.services_named('https://mediafusion.example/D-q83hf8Hf73hf73Hfh38fh3/manifest.json'), set())

    def test_a_word_that_only_contains_rd_is_not_real_debrid(self):
        self.assertEqual(realdebrid.services_named('https://third.example/ordered/torbox=KEY/manifest.json'), {'torbox'})

    def test_a_service_is_named_only_as_a_whole_word(self):
        self.assertEqual(realdebrid.services_named('https://torrinfo.example/seedrank/manifest.json'), set())
        self.assertEqual(realdebrid.services_named('https://x.example/put.io=KEY/Real-Debrid=KEY/manifest.json'),
                         {'putio', 'realdebrid'})


class InUseTest(unittest.TestCase):
    def test_the_service_list_and_linked_addons(self):
        config = {'services': [REAL_DEBRID, NZBDAV],
                  'presets': [link_addon('https://t.example/torbox=KEY/manifest.json')]}
        self.assertEqual(realdebrid.debrid_in_use(config), {'realdebrid', 'torbox'})

    def test_a_switched_off_service_is_not_in_use(self):
        self.assertEqual(realdebrid.debrid_in_use({'services': [dict(TORBOX, enabled=False), REAL_DEBRID]}), {'realdebrid'})

    def test_real_debrid_only(self):
        self.assertTrue(realdebrid.real_debrid_only({'services': [REAL_DEBRID, NZBDAV]}))
        self.assertFalse(realdebrid.real_debrid_only({'services': [REAL_DEBRID, TORBOX]}))
        self.assertFalse(realdebrid.real_debrid_only({'services': [NZBDAV]}))            # no debrid at all: another stop
        self.assertFalse(realdebrid.real_debrid_only(
            {'services': [REAL_DEBRID], 'presets': [link_addon('https://t.example/torbox=KEY/manifest.json')]}))

    def test_a_service_only_reachable_through_a_left_out_addon_does_not_count(self):
        both = link_addon('https://t.example/realdebrid=KEY|torbox=KEY/manifest.json')
        self.assertTrue(realdebrid.real_debrid_only({'services': [REAL_DEBRID], 'presets': [both]}))

    def test_the_message_gives_the_reason_and_the_way_out(self):
        self.assertTrue(realdebrid.REAL_DEBRID_ONLY.startswith("AIOSubtitles can't work with Real-Debrid only, because "))
        self.assertIn('Remote Traffic', realdebrid.REAL_DEBRID_ONLY)
        self.assertTrue(realdebrid.REAL_DEBRID_ONLY.endswith('Add another supported service, such as TorBox, to use it.'))


class WithoutRealDebridTest(unittest.TestCase):
    def test_real_debrid_comes_off_the_service_list(self):
        config = {'services': [TORBOX, dict(REAL_DEBRID, enabled=False)], 'presets': []}
        new, changes = realdebrid.without_real_debrid(config)
        self.assertEqual(new['services'], [TORBOX])
        self.assertEqual(len(changes), 1)
        self.assertIn('Real-Debrid', changes[0])
        self.assertEqual(len(config['services']), 2)                                     # the input is untouched

    def test_an_addons_own_service_choice_loses_real_debrid(self):
        comet = {'type': 'comet', 'enabled': True, 'options': {'name': 'Comet', 'services': ['realdebrid', 'torbox']}}
        new, changes = realdebrid.without_real_debrid({'services': [TORBOX], 'presets': [comet]})
        self.assertEqual(new['presets'][0]['options']['services'], ['torbox'])
        self.assertEqual(changes, ['"Comet" no longer uses Real-Debrid'])

    def test_an_addon_set_to_real_debrid_only_is_left_out(self):
        comet = {'type': 'comet', 'enabled': True, 'options': {'name': 'Comet', 'services': ['realdebrid']}}
        new, changes = realdebrid.without_real_debrid({'services': [TORBOX], 'presets': [comet]})
        self.assertEqual(new['presets'], [])
        self.assertEqual(changes, ['"Comet" left out: it is set to use Real-Debrid only'])

    def test_linked_addons(self):
        keep = link_addon('https://t.example/torbox=KEY/manifest.json', 'TorBox link')
        names_rd = link_addon('https://t.example/realdebrid=KEY|torbox=KEY/manifest.json', 'Both')
        unknown = link_addon('https://mf.example/D-q83hf8Hf73hf73Hfh38fh3/manifest.json', 'Encrypted')
        subtitles = link_addon('https://subs.example/D-q83hf8Hf73hf73Hfh38fh3/manifest.json', 'Subs', ['subtitles'])
        new, changes = realdebrid.without_real_debrid({'services': [TORBOX], 'presets': [keep, names_rd, unknown, subtitles]})
        self.assertEqual([p['options']['name'] for p in new['presets']], ['TorBox link', 'Subs'])
        self.assertEqual(changes, ['"Both" left out: its link names Real-Debrid',
                                   '"Encrypted" left out: its link doesn\'t show which debrid service it uses, '
                                   'so it could use Real-Debrid'])

    def test_a_config_without_real_debrid_needs_no_change(self):
        config = {'services': [TORBOX], 'presets': [link_addon('https://t.example/torbox=KEY/manifest.json')]}
        self.assertEqual(realdebrid.without_real_debrid(config), (config, []))


class RowTest(unittest.TestCase):
    @staticmethod
    def row(service):
        data = {'type': 'debrid', 'filename': 'a.mkv', 'size': 10}
        if service:
            data['service'] = {'id': service, 'cached': True}
        return {'url': 'http://aios/playback/x', 'streamData': data}

    def test_real_debrid_rows_are_never_read(self):
        self.assertEqual(realdebrid.row_problem(self.row('realdebrid')), 'a Real-Debrid file')
        self.assertEqual(unreadable(self.row('realdebrid')), 'a Real-Debrid file')

    def test_a_debrid_row_without_a_named_service_is_never_read(self):
        self.assertEqual(unreadable(self.row(None)), 'no named debrid service')

    def test_other_services_are_read(self):
        self.assertIsNone(realdebrid.row_problem(self.row('torbox')))


class Redirector:
    """A local server that answers every request with a redirect to `target` (counts the requests it gets)."""

    def __init__(self, target):
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                self.send_response(307)
                self.send_header('Location', target)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self.server = start_server(ThreadingHTTPServer(('127.0.0.1', 0), H))
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/file.mkv'

    def close(self):
        stop_server(self.server)


class GateTest(unittest.TestCase):
    TARGET = 'https://42.download.real-debrid.com/d/ABC/file.mkv'

    def setUp(self):
        guard = local_network_only()
        guard.start()
        self.addCleanup(guard.stop)
        self.redirector = Redirector(self.TARGET)

    def tearDown(self):
        self.redirector.close()

    def test_real_debrid_hosts(self):
        for host in ('real-debrid.com', 'api.real-debrid.com', '42.Download.Real-Debrid.com', 'real-debrid.com.'):
            self.assertTrue(realdebrid.is_real_debrid_host(host), host)
        for host in ('notreal-debrid.com', 'real-debrid.com.example.org', 'torbox.app', '', None):
            self.assertFalse(realdebrid.is_real_debrid_host(host), host)

    def test_an_address_on_real_debrid_is_refused_and_logged(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING') as logs:
            with self.assertRaises(realdebrid.Blocked):
                realdebrid.urlopen(urllib.request.Request('https://api.real-debrid.com/rest/1.0/user'), timeout=5)
        self.assertIn('blocked a request to Real-Debrid (api.real-debrid.com)', logs.output[0])
        self.assertNotIn('/rest/', logs.output[0])                                        # the host only, never the link

    def test_a_redirect_to_real_debrid_is_refused(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(realdebrid.Blocked):
                realdebrid.urlopen(self.redirector.url, timeout=5)
        self.assertEqual(self.redirector.hits, 1)

    def test_a_real_debrid_host_spelled_otherwise_is_refused(self):
        # Urllib undoes percent escapes in the host, and the resolver IDNA-encodes it (a soft hyphen goes)
        for url in ('https://42.download.real%2Ddebrid.com/d/X', 'https://42.download.real-debrid%2Ecom/d/X',
                    'https://42.download.re­al-debrid.com/d/X', 'https://42.download.RE%C2%ADAL-debrid.com/d/X',
                    'https://42.download.real-debrid。com/d/X', 'https://42.download.ｒeal-debrid.com/d/X'):
            with self.assertLogs('subsync.realdebrid', 'WARNING') as logs:
                with self.assertRaises(realdebrid.Blocked, msg=url):
                    realdebrid.urlopen(url, timeout=5)
            self.assertIn('blocked a request to Real-Debrid (42.download.real-debrid.com)', logs.output[0])
        self.redirector.close()
        self.redirector = Redirector('https://42.download.real%2Ddebrid.com/d/ABC/file.mkv')
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(realdebrid.Blocked):
                realdebrid.urlopen(self.redirector.url, timeout=5)
        self.assertEqual(self.redirector.hits, 1)

    def test_an_escaped_host_is_refused_before_urllib_connects_to_it(self):
        # end to end on this machine: "localhost" stands in for Real-Debrid's domain
        server = Redirector('http://127.0.0.1:9/never')
        self.addCleanup(server.close)
        port = server.server.server_address[1]
        with mock.patch.object(realdebrid, 'DOMAINS', ('localhost',)):
            for host in ('local%68ost', 'LOCAL%48OST', 'local­host'):
                with self.assertLogs('subsync.realdebrid', 'WARNING'):
                    with self.assertRaises(realdebrid.Blocked, msg=host):
                        realdebrid.urlopen(f'http://{host}:{port}/file.mkv', timeout=5)
        self.assertEqual(server.hits, 0)

    def test_the_reader_never_follows_a_redirect_to_real_debrid(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(ReadFailed):
                RangeReader(self.redirector.url, 'test').get(0, 1024)

    def test_subtitle_downloads_go_through_the_gate(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(realdebrid.Blocked):
                fetch_subtitle(self.redirector.url, timeout=5)

    def test_a_wrapped_address_is_checked_as_urllib_reads_it(self):
        # Urllib unwraps "<URL:…>" and "<…>" when it makes a Request of an address, which urlsplit reads as a
        # path without a host: the Real-Debrid check reads the Request's own address, so it is the one that refuses
        for url in ('<URL:https://42.download.real-debrid.com/d/X/file.mkv>', ' <URL:https://real-debrid.com/x>',
                    '<https://real-debrid.com/x>'):
            with self.assertLogs('subsync.realdebrid', 'WARNING') as logs:
                with self.assertRaises(realdebrid.Blocked, msg=url):
                    realdebrid.urlopen(url, timeout=5)
            self.assertIn('blocked a request to Real-Debrid (', logs.output[0])
        self.assertEqual(self.redirector.hits, 0)


class RedirectBodyTest(unittest.TestCase):
    def test_a_followed_redirects_own_body_is_never_read(self):
        # Urllib read a redirect's whole body before following it, with no bound on its size or time: an
        # endless one filled memory, one that stops coming held the request. Here it stops after its first bytes
        release = threading.Event()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == '/a':
                    self.send_response(302)
                    self.send_header('Location', '/b')
                    self.send_header('Content-Length', str(1 << 30))
                    self.end_headers()
                    self.wfile.write(b'x' * 65536)
                    self.wfile.flush()
                    release.wait(10)          # the rest never comes
                    return
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

        server = start_server(ThreadingHTTPServer(('127.0.0.1', 0), H))
        self.addCleanup(stop_server, server)
        self.addCleanup(release.set)
        t0 = time.monotonic()
        with local_network_only():
            with realdebrid.urlopen(f'http://127.0.0.1:{server.server_address[1]}/a', timeout=2) as r:
                self.assertEqual(r.read(), b'ok')
        self.assertLess(time.monotonic() - t0, 1.5)   # not the read's own wait (2 s) on the redirect's body


class OneConfigAiostreams:
    """Just enough of AIOStreams for the listing check: one config (a fixed id and password) behind its user API (Basic
    auth), read before every listing; a listing with one Real-Debrid and one TorBox row and one subtitle list; faults:
    `limited` (that many reads answer 429, retry at once) and `moved` (the user API redirects on the same host). Not
    helpers.FakeAiostreams, which makes configs and answers their listings; `calls` names each route asked, in order."""
    UUID = '9aaaaaaa-1234-4abc-8def-0123456789ab'
    PASSWORD = 'sync-config-password'

    def __init__(self, config):
        self.config, self.calls, self.limited = config, [], 0
        self.moved = False      # the user API answers with a redirect to another path on the same host
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = self.path.split('?')[0]
                if path == '/api/v1/user' and outer.moved:
                    outer.calls.append('config')
                    self.send_response(302)
                    self.send_header('Location', '/api/v1/user-moved')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if path in ('/api/v1/user', '/api/v1/user-moved'):
                    outer.calls.append('config' if path == '/api/v1/user' else 'moved')
                    if outer.limited:
                        outer.limited -= 1
                        self.send_response(429)
                        self.send_header('Retry-After', '0')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    sent = base64.b64decode(self.headers.get('Authorization', 'Basic ')[6:]).decode()
                    if sent != f'{outer.UUID}:{outer.PASSWORD}':
                        return self._json(401, {'success': False, 'error': {'code': 'USER_INVALID_DETAILS'}})
                    return self._json(200, {'success': True, 'data': {'userData': outer.config, 'encryptedPassword': 'E'}})
                if '/stream/' in path:
                    outer.calls.append('streams')
                    return self._json(200, {'streams': [
                        {'url': 'http://aios/playback/1', 'streamData': {'type': 'debrid', 'filename': 'a.mkv', 'size': 10,
                                                                         'service': {'id': 'realdebrid', 'cached': True}}},
                        {'url': 'http://aios/playback/2', 'streamData': {'type': 'debrid', 'filename': 'a.mkv', 'size': 10,
                                                                         'service': {'id': 'torbox', 'cached': True}}}]})
                if '/subtitles/' in path:
                    outer.calls.append('subtitles')
                    return self._json(200, {'subtitles': [{'id': 's1', 'url': 'http://subs/1.srt', 'lang': 'eng'}]})
                return self._json(404, {})

        self.server = start_server(ThreadingHTTPServer(('127.0.0.1', 0), H))
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.manifest = f'{self.base}/stremio/{self.UUID}/ENCRYPTED/manifest.json'

    def close(self):
        stop_server(self.server)


class ListingCheckTest(unittest.TestCase):
    """Before every request through subsync's AIOStreams link, the config behind it is read and checked: AIOStreams
    searches and checks every service in a config while it answers a listing, so the check comes first."""
    SAFE = {'services': [TORBOX], 'presets': []}

    def setUp(self):
        guard = local_network_only()
        guard.start()
        self.addCleanup(guard.stop)

    def aios(self, config, password=OneConfigAiostreams.PASSWORD):
        self.fake = OneConfigAiostreams(config)
        self.addCleanup(self.fake.close)
        return Aios(self.fake.manifest, password=password, timeout=5)

    def test_every_listing_reads_the_config_first(self):
        aios = self.aios(self.SAFE)
        aios.streams('movie', 'tt1')
        aios.subtitles('movie', 'tt1', {})
        self.assertEqual(self.fake.calls, ['config', 'streams', 'config', 'subtitles'])

    def test_a_config_that_could_reach_real_debrid_is_never_listed(self):
        aios = self.aios({'services': [TORBOX, REAL_DEBRID], 'presets': []})
        with self.assertLogs('subsync.sources', 'WARNING') as logs:
            with self.assertRaises(realdebrid.Blocked):
                aios.streams('movie', 'tt1')
            with self.assertRaises(realdebrid.Blocked):
                aios.subtitles('movie', 'tt1', {})
        self.assertEqual(self.fake.calls, ['config', 'config'])
        self.assertIn('Real-Debrid taken off the service list', logs.output[0])
        self.assertIn('./setup', logs.output[0])

    def test_a_config_that_cant_be_read_is_never_listed(self):
        aios = self.aios(self.SAFE, password='wrong')
        with self.assertLogs('subsync.sources', 'WARNING') as logs:
            with self.assertRaises(realdebrid.Blocked):
                aios.streams('movie', 'tt1')
        self.assertEqual(self.fake.calls, ['config'])
        self.assertIn("can't read", logs.output[0])
        self.assertNotIn('wrong', logs.output[0])

    def test_why_the_config_cant_be_read_is_logged_by_its_code_as_before(self):
        # An AIOStreams that doesn't answer has a code of its own now (UNREACHABLE); its line still says
        # "no answer", and a refusal still names AIOStreams' code
        dead = Aios(f'http://127.0.0.1:9/stremio/{OneConfigAiostreams.UUID}/ENCRYPTED/manifest.json',
                    password=OneConfigAiostreams.PASSWORD, timeout=2)
        for aios, reason in ((dead, 'no answer'), (self.aios(self.SAFE, password='wrong'), 'USER_INVALID_DETAILS')):
            with self.subTest(reason=reason):
                with self.assertLogs('subsync.sources', 'WARNING') as logs:
                    with self.assertRaises(realdebrid.Blocked):
                        aios.streams('movie', 'tt1')
                self.assertIn(f"can't read subsync's config ({reason}), so nothing is listed", logs.output[0])

    def test_an_error_code_that_isnt_one_is_never_logged(self):
        # AIOStreams' error code was logged as it came, any length and any characters, so a person's own
        # AIOStreams (public mode) could write lines of its own into the operator's log, megabytes of them. A code is
        # named only when it is an AIOStreams-style token, a status only when it is a number
        from subsync.aiosconfig import UNREACHABLE, AiosError
        forged = 'X\n2026-11-19 00:00:00,000 WARNING subsync.realdebrid forged line\n' + 'A' * 200_000
        for code, named in ((forged, 'an error answer'), ('A' * 65, 'an error answer'), ('bad code', 'an error answer'),
                            (['USER_INVALID_DETAILS'], 'an error answer'), ({'code': 1}, 'an error answer'),
                            (1.5, 'an error answer'), (True, 'an error answer'), ('A' * 64, 'A' * 64),
                            ('USER_INVALID_DETAILS', 'USER_INVALID_DETAILS'), (401, 'HTTP 401'), (None, 'no answer'),
                            ('', 'no answer'), (UNREACHABLE, 'no answer')):
            with self.subTest(code=repr(code)[:30]):
                aios = Aios(f'http://127.0.0.1:9/stremio/{OneConfigAiostreams.UUID}/ENCRYPTED/manifest.json',
                            password=OneConfigAiostreams.PASSWORD)
                aios._read_config = mock.Mock(side_effect=AiosError('refused', code=code))
                with self.assertLogs('subsync.sources', 'WARNING') as logs:
                    with self.assertRaises(realdebrid.Blocked):
                        aios.check_config()
                self.assertEqual(logs.output, [f"WARNING:subsync.sources:Real-Debrid check: can't read subsync's "
                                               f"config ({named}), so nothing is listed through it"])

    def test_a_rate_limited_config_read_is_tried_again(self):
        aios = self.aios(self.SAFE)
        self.fake.limited = 1
        aios.streams('movie', 'tt1')
        self.assertEqual(self.fake.calls, ['config', 'config', 'streams'])

    def test_the_config_read_with_its_password_follows_no_redirect(self):
        # The config was read by a second, hand-written AIOStreams client, which followed a redirect on the
        # same host with the password; it is read by aiosconfig's client now, whose calls with a password follow none
        aios = self.aios(self.SAFE)
        self.fake.moved = True
        with self.assertLogs('subsync.sources', 'WARNING') as logs:
            with self.assertRaises(realdebrid.Blocked):
                aios.streams('movie', 'tt1')
        self.assertEqual(self.fake.calls, ['config'])
        self.assertIn("can't read", logs.output[0])
        self.assertNotIn(OneConfigAiostreams.PASSWORD, logs.output[0])

    def test_without_its_password_nothing_is_listed(self):
        aios = self.aios(self.SAFE, password='')
        with self.assertLogs('subsync.sources', 'WARNING'):
            with self.assertRaises(realdebrid.Blocked):
                aios.streams('movie', 'tt1')
        self.assertEqual(self.fake.calls, [])

    def test_a_listed_real_debrid_file_is_skipped_and_logged(self):
        aios = self.aios(self.SAFE)
        with self.assertLogs('subsync.sources', 'WARNING') as logs:
            row = aios.find_row('movie', 'tt1', {'filename': 'a.mkv'})
        self.assertEqual(row['url'], 'http://aios/playback/2')
        self.assertIn('1 listed Real-Debrid file skipped', logs.output[0])

    def test_subsync_will_not_start_without_the_password(self):
        self.assertIn('SUBSYNC_SOURCE_PASSWORD', Settings({}).missing())
        self.assertEqual(Settings({'SUBSYNC_SOURCE_PASSWORD': 'p'}).source_password, 'p')


if __name__ == '__main__':
    unittest.main()
