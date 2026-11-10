"""The installer's rules, one by one (python3 -m unittest discover -s installer/tests -t .)."""
import copy
import unittest

from installer import plan

META = {
    'custom': {'OPTIONS': [{'id': 'name', 'default': 'Custom Addon'}, {'id': 'timeout', 'default': 7000},
                           {'id': 'libraryAddon', 'default': False}, {'id': 'resultPassthrough', 'default': False}]},
    'opensubtitles-v3-plus': {'OPTIONS': [{'id': 'language', 'options': [{'value': 'en'}, {'value': 'ru'}]},
                                          {'id': 'movieHashPlusAutoAdjustment', 'default': False}]},
}
VIEWER = {
    'services': [{'id': 'torbox', 'enabled': True, 'credentials': {'apiKey': 'k'}},
                 {'id': 'aiostreams', 'enabled': True, 'credentials': {}},
                 {'id': 'realdebrid', 'enabled': True, 'credentials': {'apiKey': 'r'}}],
    'presets': [{'type': 'zilean', 'instanceId': 'zil', 'enabled': True, 'options': {'name': 'Zilean'}},
                {'type': 'newznab', 'instanceId': 'nzb', 'enabled': True, 'options': {'name': 'Indexer'}},
                {'type': 'opensubtitles-v3-plus', 'instanceId': 'osp', 'enabled': True,
                 'options': {'name': 'OpenSubtitles V3+', 'resources': ['subtitles']}}],
    'failover': {'enabled': True, 'attempts': 3},
    'preloadStreams': {'enabled': True, 'selector': 'x'},
    'precacheNextEpisode': True,
    'uuid': 'u', 'accessKey': 'a',
}
URL = 'http://aiosubtitles-subsync:8790/KEY/manifest.json'


class Languages(unittest.TestCase):
    def test_names_and_codes(self):
        langs, error = plan.parse_languages('English, ru; German')
        self.assertIsNone(error)
        self.assertEqual([t for _, _, t in langs], ['eng', 'rus', 'ger'])
        self.assertEqual(plan.parse_languages('deu')[0][0][2], 'ger')

    def test_mistakes(self):
        self.assertIn('Klingon', plan.parse_languages('English, Klingon')[1])
        self.assertTrue(plan.parse_languages('')[1])
        self.assertTrue(plan.parse_languages('en es fr de it')[1])
        self.assertEqual(len(plan.parse_languages('en, english, eng')[0]), 1)


class ConfigId(unittest.TestCase):
    def test_from_link_or_id(self):
        u = '1a2b3c4d-1234-4abc-8def-0123456789ab'
        self.assertEqual(plan.config_id_from(f'https://aio.example.com/stremio/{u}/ENC/manifest.json'), u)
        self.assertEqual(plan.config_id_from(u.upper()), u)
        self.assertIsNone(plan.config_id_from('not one'))


class ViewerChange(unittest.TestCase):
    def test_adds_subsync_once_with_defaults(self):
        new, changes = plan.viewer_change(VIEWER, URL, META)
        added = [p for p in new['presets'] if p['type'] == 'custom']
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['options']['manifestUrl'], URL)
        self.assertEqual(added[0]['options']['resources'], ['subtitles'])
        self.assertEqual(added[0]['options']['timeout'], 2000)          # ours, not the preset default
        self.assertIs(added[0]['options']['libraryAddon'], False)        # preset default filled in
        self.assertEqual(added[0]['instanceId'], 'ssy')
        self.assertEqual(len(changes), 1)
        self.assertEqual(VIEWER['presets'][-1]['type'], 'opensubtitles-v3-plus')   # the input is untouched
        again, changes = plan.viewer_change(new, URL, META)
        self.assertEqual(changes, [])
        self.assertEqual(again, new)

    def test_new_address_replaces_the_old_entry(self):
        old, _ = plan.viewer_change(VIEWER, 'http://old:8790/OLD/manifest.json', META)
        new, changes = plan.viewer_change(old, URL, META)
        self.assertEqual([p['options']['manifestUrl'] for p in new['presets'] if p['type'] == 'custom'], [URL])
        self.assertIn('updates', changes[0])

    def test_opensubtitles_offer_is_the_official_one(self):
        # Stremio's official OpenSubtitles (preset `opensubtitles`), not the slow community V3+
        bare = {'services': VIEWER['services'], 'presets': VIEWER['presets'][:1]}
        meta = dict(META, opensubtitles={'OPTIONS': [{'id': 'name', 'default': 'OpenSubtitles'}, {'id': 'timeout', 'default': 7000}]})
        new, changes = plan.viewer_change(bare, URL, meta, True)
        os_ = [p for p in new['presets'] if p['type'] == 'opensubtitles']
        self.assertEqual(len(os_), 1)
        self.assertEqual((os_[0]['instanceId'], os_[0]['options']['resources']), ('ost', ['subtitles']))
        self.assertNotIn('language', os_[0]['options'])
        self.assertFalse([p for p in new['presets'] if p['type'] == 'opensubtitles-v3-plus'])
        self.assertEqual(len(changes), 2)
        self.assertEqual(plan.subtitle_sources(new, URL), ['OpenSubtitles'])
        self.assertEqual(plan.viewer_change(bare, URL, meta)[0]['presets'][:1], bare['presets'])   # not asked: not added

    def test_uninstall(self):
        new, _ = plan.viewer_change(VIEWER, URL, META)
        back, removed = plan.without_subsync(new, URL)
        self.assertEqual((removed, back['presets']), (1, VIEWER['presets']))


class SyncConfig(unittest.TestCase):
    def test_without_usenet_subsync_and_background_work(self):
        viewer, _ = plan.viewer_change(VIEWER, URL, META)
        sync = plan.sync_config(viewer, URL)
        self.assertEqual([p['type'] for p in sync['presets']], ['zilean', 'opensubtitles-v3-plus'])
        self.assertEqual([s['id'] for s in sync['services']], ['torbox', 'realdebrid'])
        self.assertEqual(sync['failover'], {'enabled': False, 'attempts': 3, 'precacheFailover': False})
        self.assertEqual(sync['preloadStreams'], {'enabled': False, 'selector': 'x'})
        self.assertIs(sync['precacheNextEpisode'], False)
        self.assertIs(sync['checkOwned'], False)
        self.assertNotIn('uuid', sync)
        self.assertNotIn('accessKey', sync)
        self.assertEqual(plan.sync_config_problems(sync), [])

    def test_usenet_only_config_is_refused(self):
        usenet = {'services': [{'id': 'nzbdav', 'enabled': True}], 'presets': [VIEWER['presets'][1]]}
        self.assertEqual(len(plan.sync_config_problems(plan.sync_config(usenet))), 2)


class RealDebrid(unittest.TestCase):
    def test_direct_or_proxied(self):
        cfg = copy.deepcopy(VIEWER)
        self.assertTrue(plan.real_debrid_direct(cfg, {}))
        cfg['proxy'] = {'enabled': True, 'id': 'builtin'}
        self.assertFalse(plan.real_debrid_direct(cfg, {}))
        cfg['proxy']['proxiedServices'] = ['torbox']
        self.assertTrue(plan.real_debrid_direct(cfg, {}))
        forced = {'forced': {'proxy': {'enabled': True, 'proxiedServices': None}}}
        self.assertFalse(plan.real_debrid_direct(copy.deepcopy(VIEWER), forced))
        self.assertFalse(plan.real_debrid_direct({'services': [{'id': 'torbox'}]}, {}))


class Addresses(unittest.TestCase):
    def test_public_default(self):
        self.assertEqual(plan.public_default('https://aio.example.com', '192.168.1.5'), ('path', 'https://aio.example.com/subsync'))
        self.assertEqual(plan.public_default('http://192.168.1.5:3000', '10.0.0.2'), ('port', 'http://192.168.1.5:8790'))
        self.assertEqual(plan.public_default('http://localhost:3000', '192.168.1.5'), ('port', 'http://192.168.1.5:8790'))
        self.assertEqual(plan.public_default('https://nas.local', '192.168.1.5'), ('port', 'http://nas.local:8790'))

    def test_checks_and_rules(self):
        self.assertEqual(plan.check_public('https://a.example.com/subsync/'), (True, '', 'https://a.example.com/subsync'))
        self.assertFalse(plan.check_public('a.example.com')[0])
        self.assertEqual(plan.base_path_of('https://a.example.com/subsync'), '/subsync')
        self.assertEqual(plan.base_path_of('http://1.2.3.4:8790'), '')
        self.assertEqual(plan.rewrite_rule('https://a.example.com/', 'http://aiostreams:3000'),
                         'https://a.example.com=http://aiostreams:3000')
        self.assertEqual(plan.rewrite_rule('http://x:1', 'http://x:1'), '')

    def test_subtitle_cache_value(self):
        self.assertEqual(plan.subtitle_cache_value({'*': 600}, 'aiosubtitles-subsync'), {'*': 600, 'aiosubtitles-subsync': -1})
        self.assertEqual(plan.subtitle_cache_value('*:300,other:10', 'h'), {'*': 300, 'other': 10, 'h': -1})
        self.assertEqual(plan.subtitle_cache_value(None, 'h'), {'*': 300, 'h': -1})


class Files(unittest.TestCase):
    S = {'sync_manifest': 'http://aiostreams:3000/stremio/u/e/manifest.json', 'public_base': 'https://a.example.com/subsync',
         'addon_key': 'K', 'languages': plan.parse_languages('en, ru')[0], 'rewrite': 'a=b', 'audio': False}

    def test_env(self):
        env = plan.subsync_env(self.S)
        self.assertEqual(env['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertEqual(env['SUBSYNC_BASE_PATH'], '/subsync')
        self.assertEqual(env['SUBSYNC_AUDIO_ROUTE'], 'off')
        self.assertEqual(sorted(env), sorted(['SUBSYNC_SOURCE_MANIFEST', 'SUBSYNC_PUBLIC_BASE', 'SUBSYNC_BASE_PATH', 'SUBSYNC_ADDON_KEY',
                                             'SUBSYNC_LANGUAGES', 'SUBSYNC_URL_REWRITE', 'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_DAILY_BUDGET_GB']))
        env = plan.subsync_env(dict(self.S, audio=True, public_base='http://1.2.3.4:8790', rewrite=''))
        self.assertEqual(env['SUBSYNC_AUDIO_ROUTE'], 'on')
        self.assertNotIn('SUBSYNC_BASE_PATH', env)
        self.assertNotIn('SUBSYNC_URL_REWRITE', env)

    def test_compose(self):
        text = plan.compose_yaml({'version': '1.0.0', 'networks': ['aio_default', 'proxy'], 'bind': '127.0.0.1', 'port': 8790})
        self.assertIn('ports: ["127.0.0.1:8790:8790"]', text)
        self.assertIn('aios1: {aliases: [aiosubtitles-subsync]}', text)
        self.assertIn('aios0: {external: true, name: "aio_default"}', text)
        host = plan.compose_yaml({'version': '1.0.0', 'host_network': True, 'port': 8790})
        self.assertIn('network_mode: host', host)
        self.assertNotIn('ports:', host)
        self.assertNotIn('networks:', host)

    def test_proxy_snippets_keep_the_path(self):
        s = plan.proxy_snippets('https://a.example.com/subsync', '127.0.0.1:8790')
        self.assertIn('    reverse_proxy /subsync/* 127.0.0.1:8790', s['Caddy'])
        self.assertIn('    proxy_pass http://127.0.0.1:8790;', s['nginx'])


if __name__ == '__main__':
    unittest.main()
