"""Real-Debrid is never used: the config subsync lists through, each listed row, and every web request (local fakes
only; nothing here reaches a debrid service)."""
import base64
import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import local_network_only  # noqa: E402
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

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/file.mkv'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


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

    def test_the_reader_never_follows_a_redirect_to_real_debrid(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(ReadFailed):
                RangeReader(self.redirector.url, 'test').get(0, 1024)

    def test_subtitle_downloads_go_through_the_gate(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(realdebrid.Blocked):
                fetch_subtitle(self.redirector.url, timeout=5)


class FakeAiostreams:
    """Just enough of AIOStreams: its user API (Basic auth) and one config's stream and subtitle routes."""
    UUID = '9aaaaaaa-1234-4abc-8def-0123456789ab'
    PASSWORD = 'sync-config-password'

    def __init__(self, config):
        self.config, self.calls, self.limited = config, [], 0
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
                if path == '/api/v1/user':
                    outer.calls.append('config')
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

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.manifest = f'{self.base}/stremio/{self.UUID}/ENCRYPTED/manifest.json'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ListingCheckTest(unittest.TestCase):
    """Before every request through subsync's AIOStreams link, the config behind it is read and checked: AIOStreams
    searches and checks every service in a config while it answers a listing, so the check comes first."""
    SAFE = {'services': [TORBOX], 'presets': []}

    def setUp(self):
        guard = local_network_only()
        guard.start()
        self.addCleanup(guard.stop)

    def aios(self, config, password=FakeAiostreams.PASSWORD):
        self.fake = FakeAiostreams(config)
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

    def test_a_rate_limited_config_read_is_tried_again(self):
        aios = self.aios(self.SAFE)
        self.fake.limited = 1
        aios.streams('movie', 'tt1')
        self.assertEqual(self.fake.calls, ['config', 'config', 'streams'])

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
