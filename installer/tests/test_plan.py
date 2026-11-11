"""The installer's rules, one by one (python3 -m unittest discover -s installer/tests -t .)."""
import copy
import unittest

from installer import plan
from subsync import explain, realdebrid

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
    def kinds(self, config):
        return ['subsync' if plan.is_subsync_preset(p, URL) else p['type'] for p in config['presets']
                if plan.is_subtitle_preset(p)]

    def test_subsync_comes_before_the_other_subtitle_sources(self):
        new, _ = plan.viewer_change(VIEWER, URL, META)
        self.assertEqual(self.kinds(new)[0], 'subsync')
        self.assertEqual(len(self.kinds(new)), len(self.kinds(VIEWER)) + 1)
        streams = [p for p in VIEWER['presets'] if not plan.is_subtitle_preset(p)]
        self.assertEqual([p for p in new['presets'] if not plan.is_subtitle_preset(p)], streams)   # the rest keeps its order

    def test_an_install_that_has_subsync_after_the_others_is_moved_once(self):
        old = dict(VIEWER, presets=list(VIEWER['presets']) + [plan.subsync_preset(VIEWER, URL, META)])
        self.assertEqual(self.kinds(old)[-1], 'subsync')
        new, changes = plan.viewer_change(old, URL, META)
        self.assertEqual(self.kinds(new)[0], 'subsync')
        self.assertEqual(len(changes), 1)
        self.assertIn('moves "subsync" before', changes[0])
        self.assertEqual(sorted(map(str, new['presets'])), sorted(map(str, old['presets'])))        # nothing added or lost
        self.assertEqual(plan.viewer_change(new, URL, META)[1], [])

    def test_with_no_subtitle_source_subsync_is_added_then_opensubtitles(self):
        bare = dict(VIEWER, presets=[p for p in VIEWER['presets'] if not plan.is_subtitle_preset(p)])
        new, changes = plan.viewer_change(bare, URL, META, True)
        self.assertEqual(self.kinds(new), ['subsync', 'opensubtitles'])
        self.assertEqual(len(changes), 2)

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
        self.assertEqual([s['id'] for s in sync['services']], ['torbox'])
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
    def test_the_sync_config_has_no_way_to_real_debrid(self):
        viewer = copy.deepcopy(VIEWER)
        viewer['presets'] += [
            {'type': 'comet', 'instanceId': 'com', 'enabled': True, 'options': {'name': 'Comet', 'services': ['realdebrid', 'torbox']}},
            {'type': 'custom', 'instanceId': 'tio', 'enabled': True,
             'options': {'name': 'Torrentio', 'manifestUrl': 'https://torrentio.example/realdebrid=KEY/manifest.json'}}]
        sync = plan.sync_config(viewer, URL)
        self.assertEqual(realdebrid.without_real_debrid(sync)[1], [])
        self.assertEqual([p['options']['name'] for p in sync['presets']], ['Zilean', 'OpenSubtitles V3+', 'Comet'])
        self.assertEqual(sync['presets'][-1]['options']['services'], ['torbox'])

    def test_a_service_named_in_an_addon_link_counts_for_the_sync_config(self):
        linked = {'services': [], 'presets': [{'type': 'custom', 'enabled': True, 'options': {
            'name': 'Torrentio', 'manifestUrl': 'https://torrentio.example/torbox=KEY/manifest.json'}}]}
        self.assertEqual(plan.sync_config_problems(plan.sync_config(linked)), [])


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
    S = {'sync_manifest': 'http://aiostreams:3000/stremio/u/e/manifest.json', 'sync_password': 'P',
         'public_base': 'https://a.example.com/subsync',
         'addon_key': 'K', 'languages': plan.parse_languages('en, ru')[0], 'rewrite': 'a=b', 'audio': False}

    def test_env(self):
        env = plan.subsync_env(self.S)
        self.assertEqual(env['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertEqual(env['SUBSYNC_BASE_PATH'], '/subsync')
        self.assertEqual(env['SUBSYNC_AUDIO_ROUTE'], 'off')
        self.assertEqual(env['SUBSYNC_SOURCE_PASSWORD'], 'P')
        self.assertEqual(sorted(env), sorted(['SUBSYNC_SOURCE_MANIFEST', 'SUBSYNC_SOURCE_PASSWORD', 'SUBSYNC_PUBLIC_BASE',
                                             'SUBSYNC_BASE_PATH', 'SUBSYNC_ADDON_KEY',
                                             'SUBSYNC_LANGUAGES', 'SUBSYNC_URL_REWRITE', 'SUBSYNC_AUDIO_ROUTE']))
        self.assertLessEqual(set(env), set(explain.WRITTEN_BY_SETUP))          # ./setup writes only the lines it owns
        env = plan.subsync_env(dict(self.S, audio=True, public_base='http://1.2.3.4:8790', rewrite=''))
        self.assertEqual(env['SUBSYNC_AUDIO_ROUTE'], 'on')
        self.assertNotIn('SUBSYNC_BASE_PATH', env)
        self.assertNotIn('SUBSYNC_URL_REWRITE', env)

    def test_hand_added_lines_are_kept(self):
        written = plan.subsync_env(self.S)
        current = plan.parse_env('# a note\nSUBSYNC_MAX_TRIES=9   # mine\nSUBSYNC_EXTRA_SOURCES=\nTZ=Europe/Berlin\n'
                                 'SUBSYNC_LANGUAGES=fre\nSUBSYNC_PORT=9000\nnot a line\n')
        self.assertEqual(current['SUBSYNC_EXTRA_SOURCES'], '')                 # an empty value means something: kept
        merged, kept = plan.merge_env(written, current)
        self.assertEqual(kept, ['SUBSYNC_MAX_TRIES', 'SUBSYNC_EXTRA_SOURCES', 'TZ'])
        self.assertEqual(merged['SUBSYNC_MAX_TRIES'], '9')
        self.assertEqual(merged['SUBSYNC_EXTRA_SOURCES'], '')
        self.assertEqual(merged['TZ'], 'Europe/Berlin')
        self.assertEqual(merged['SUBSYNC_LANGUAGES'], 'eng,rus')             # ./setup's answer wins on the lines it owns
        self.assertNotIn('SUBSYNC_PORT', merged)                              # an owned line ./setup no longer writes goes
        self.assertEqual(list(merged)[:len(written)], list(written))          # ./setup's lines first, yours after

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
